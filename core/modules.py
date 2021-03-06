from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch.nn as nn
from torch.nn import functional as F
import torch
from core.utils.opts import time_to_batch, batch_to_time
# from core.attention_conv import AttentionConv


def get_padding(kernel, dilation):
    k2 = kernel + (kernel-1) * (dilation-1)
    return k2//2


class SequenceWise(nn.Module):
    def __init__(self, module, parallel=True):
        """
        Collapses input of dim T*N*H to (T*N)*H, and applies to a module.
        Allows handling of variable sequence lengths and minibatch sizes.
        If RNN (has reset module), will bypass reshapes
        :param module: Module to apply input to.
        :param parallel: Run in Parallel, or Sequentially. Not equivalent for BatchNorm for instance.
        """
        super(SequenceWise, self).__init__()
        self.module = module
        self.forward = self.forward_parallel if parallel else self.forward_sequential

    def forward_parallel(self, x):
        t, n = x.size(0), x.size(1)
        x = time_to_batch(x)[0]
        x = self.module(x)
        x = batch_to_time(x, n)
        return x

    def forward_sequential(self, x):
        xiseq = x.split(1, 0)
        res = []
        for t, xt in enumerate(xiseq):
            y = self.module(xt[0])
            res.append(y.unsqueeze(0))
        return torch.cat(res, dim=0)

    def __repr__(self):
        tmpstr = self.__class__.__name__ + ' (\n'
        tmpstr += self.module.__repr__()
        tmpstr += ')'
        return tmpstr


class CoordConv(nn.Module):
    def __init__(self, in_channels, out_channels, conv_func, **kwargs):
        super(CoordConv, self).__init__()
        self.conv = conv_func(in_channels + 2, out_channels, **kwargs)

    def forward(self, x):
        grid_h, grid_w = torch.meshgrid([torch.linspace(-1, 1., x.shape[2]), torch.linspace(-1, 1., x.shape[3])])
        grid = torch.cat((grid_h[None, None, :, :], grid_w[None, None, :, :]), 1).repeat(x.shape[0], 1, 1, 1)
        ret = torch.cat((x, grid.to(x.device)), 1)
        ret = self.conv(ret)
        return ret



class SeparableConv2d(nn.Sequential):
    def __init__(self, in_channels, out_channels, kernel_size=1, stride=1, padding=0, dilation=1, bias=False):
        super(SeparableConv2d, self).__init__(
            nn.Conv2d(in_channels, in_channels, kernel_size, stride, padding, dilation, groups=in_channels,
                      bias=bias),
            nn.Conv2d(in_channels, out_channels, 1, 1, 0, 1, 1, bias=bias)
        )
        

class ConvLayer(nn.Sequential):

    def __init__(self, in_channels, out_channels,
                 kernel_size=3, stride=1, padding=1, dilation=1,
                 bias=True, norm='BatchNorm2d', activation='LeakyReLU', separable=False):

        conv_func = SeparableConv2d if separable else nn.Conv2d
        super(ConvLayer, self).__init__(
            nn.Identity() if norm == 'none' else getattr(nn, norm)(in_channels),
            conv_func(in_channels, out_channels, kernel_size=kernel_size, stride=stride, dilation=dilation,
                      padding=padding, bias=bias),
            getattr(nn, activation)()
        )


class Bottleneck(nn.Module):
    def __init__(self, in_planes, planes, stride=1):
        super(Bottleneck, self).__init__()
        mid_planes = planes//4
        self.conv1 = ConvLayer(in_channels=in_planes, out_channels=mid_planes, kernel_size=1, padding=0, bias=False)
        self.conv2 = ConvLayer(in_channels=mid_planes, out_channels=mid_planes, kernel_size=3, stride=stride, padding=1,
                               bias=False)
        self.conv3 = ConvLayer(in_channels=mid_planes, out_channels=planes, kernel_size=1, padding=0, bias=False)

        self.downsample = nn.Sequential()
        if stride != 1 or in_planes != planes:
            self.downsample = ConvLayer(in_channels=in_planes, out_channels=planes,
                                     kernel_size=1, padding=0, stride=stride,
                          bias=False, activation='Identity')


    def forward(self, x):
        out = self.conv1(x)
        out = self.conv2(out)
        out = self.conv3(out)
        out += self.downsample(x)
        out = F.relu(out)
        return out


class PreActBlock(nn.Module):
    def __init__(self, in_planes, planes, stride=1):
        super(PreActBlock, self).__init__()
        self.conv1 = ConvLayer(in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.conv2 = ConvLayer(planes, planes, kernel_size=3, stride=1, padding=1, bias=False)

        self.downsample = nn.Sequential()
        if stride != 1 or in_planes != planes:
            self.downsample = nn.Sequential(
                nn.Conv2d(in_planes, planes, kernel_size=1, stride=stride, bias=False)
            )

        # SE layers
        self.fc1 = nn.Conv2d(planes, planes // 4, kernel_size=1)
        self.fc2 = nn.Conv2d(planes // 4, planes, kernel_size=1)

    def forward(self, x):
        out = self.conv1(x)
        out = self.conv2(out)

        # Squeeze
        w = F.avg_pool2d(out, out.size(2))
        w = F.relu(self.fc1(w))
        w = torch.sigmoid(self.fc2(w))

        # Excitation
        out = out * w
        out += self.downsample(x) 
        return out
        

class ASPP(nn.Module):
    def __init__(self, in_channels, out_channels, 
                kernel_size=3, atrous_rates=[3, 6, 12, 18], conv_func=nn.Conv2d):
        super(ASPP, self).__init__()
        modules = []
        for rate in atrous_rates:
            padding = get_padding(kernel_size, rate)
            modules.append(conv_func(in_channels, in_channels, kernel_size=kernel_size, groups=in_channels, dilation=rate, padding=padding))

        self.convs = nn.ModuleList(modules)
        self.project = conv_func(in_channels * len(self.convs), out_channels, kernel_size=1, padding=0)

    def forward(self, x):
        res = []
        for conv in self.convs:
            res.append(conv(x))
        res = torch.cat(res, dim=1)
        res = self.project(res)
        return res


def sequence_upsample(x, y):
    x, n = time_to_batch(x)
    x = F.interpolate(x, size=y.shape[-2:], mode='nearest')
    x = batch_to_time(x, n)
    return x



class RNNCell(nn.Module):
    """
    base class that has memory, each class with hidden state has to derive from basernn
    TODO: add the saturation cost as a hook!!

    """

    def __init__(self, hard):
        super(RNNCell, self).__init__()
        self.saturation_cost = 0
        self.saturation_limit = 1.05
        self.saturation_weight = 1e-1
        self.set_gates(hard)

    def set_gates(self, hard):
        if hard:
            self.sigmoid = self.hard_sigmoid
            self.tanh = self.hard_tanh
        else:
            self.sigmoid = torch.sigmoid
            self.tanh = torch.tanh

    def hard_sigmoid(self, x_in):
        self.add_saturation_cost(x_in)
        x = x_in * 0.5 + 0.5
        y = torch.clamp(x, 0.0, 1.0)
        return y

    def hard_tanh(self, x):
        self.add_saturation_cost(x)
        y = torch.clamp(x, -1.0, 1.0)
        return y

    def add_saturation_cost(self, var):
        """Calculate saturation cost."""
        sat_loss = F.relu(torch.abs(var) - self.saturation_limit)
        cost = sat_loss.mean()

        cost *= self.saturation_weight
        self.saturation_cost += cost

    def reset(self):
        raise NotImplementedError()


class ConvLSTMCell(RNNCell):
    r"""ConvLSTMCell module, applies sequential part of LSTM.
    """

    def __init__(self, hidden_dim, kernel_size, conv_func=nn.Conv2d, hard=False):
        super(ConvLSTMCell, self).__init__(hard)
        self.hidden_dim = hidden_dim

        self.conv_h2h = conv_func(in_channels=self.hidden_dim,
                                  out_channels=4 * self.hidden_dim,
                                  kernel_size=kernel_size,
                                  #dilation=1,
                                  groups=4,
                                  padding=kernel_size//2,
                                  bias=True)
        
        #self.conv_h2h = ASPP(self.hidden_dim, 4 * self.hidden_dim, atrous_rates=[1,2,4])

        self.reset()

    def forward(self, xi):
        self.saturation_cost = 0
        inference = (len(xi.shape) == 4)  # inference for model conversion
        if inference:
            xiseq = [xi]
        else:
            xiseq = xi.split(1, 0)  # t,n,c,h,w

        if self.prev_h is not None:
            self.prev_h = self.prev_h.detach()
            self.prev_c = self.prev_c.detach()
        else:
            shape = list(xiseq[0][0].shape)
            shape[1] = self.hidden_dim
            self.prev_h = torch.zeros(shape, dtype=torch.float32, device=xi.device)
            self.prev_c = torch.zeros(shape, dtype=torch.float32, device=xi.device)

        result = []
        for t, xt in enumerate(xiseq):
            if not inference:  # for training/val (not inference)
                xt = xt.squeeze(0)

            if self.prev_h is not None:
                tmp = self.conv_h2h(self.prev_h) + xt
            else:
                tmp = xt

            cc_i, cc_f, cc_o, cc_g = torch.split(tmp, self.hidden_dim, dim=1)
            i = self.sigmoid(cc_i)
            f = self.sigmoid(cc_f)
            o = self.sigmoid(cc_o)
            g = self.tanh(cc_g)
            if self.prev_c is None:
                c = i * g
            else:
                c = f * self.prev_c + i * g
            h = o * self.tanh(c)
            if not inference:
                result.append(h.unsqueeze(0))
            self.prev_h = h
            self.prev_c = c
        if inference:
            res = h
        else:
            res = torch.cat(result, dim=0)
        return res

    def reset(self, mask=None):
        """To be called in between batches"""
        if mask is None or self.prev_h is None:
            self.prev_h, self.prev_c = None, None
        else:
            self.prev_h, self.prev_c = self.prev_h.detach(), self.prev_c.detach()
            mask = mask.to(self.prev_h)
            self.prev_h *= mask
            self.prev_c *= mask


class ConvALSTMCell(RNNCell):
    r"""ConvLSTMCell with Attention
    Temporally Identity-Aware SSD with Attentional-LSTM
    https://arxiv.org/pdf/1803.00197.pdf

    """

    def __init__(self, in_channels, hidden_dim, kernel_size, conv_func=nn.Conv2d, hard=False):
        super(ConvALSTMCell, self).__init__(hard)
        self.hidden_dim = hidden_dim

        self.conv_x2a = ConvLayer(in_channels=in_channels,
                                  out_channels=1,
                                  kernel_size=3,
                                  dilation=1,
                                  padding=1,
                                  bias=True)

        self.conv_h2a = conv_func(in_channels=self.hidden_dim,
                                  out_channels=1,
                                  kernel_size=3,
                                  dilation=1,
                                  padding=1,
                                  bias=True)

        self.conv_ax2h = ConvLayer(in_channels=in_channels,
                                  out_channels=4 * self.hidden_dim,
                                  kernel_size=3,
                                  dilation=1,
                                  padding=1,
                                  bias=True)

        self.conv_h2h = conv_func(in_channels=self.hidden_dim,
                                  out_channels=4 * self.hidden_dim,
                                  kernel_size=3,
                                  dilation=1,
                                  padding=1,
                                  bias=True)

        # self.conv_h2h = ASPP(self.hidden_dim, 4 * self.hidden_dim, atrous_rates=[1,2,4])

        self.reset()
        self.gate_a = None

    def forward(self, xi):
        self.saturation_cost = 0
        inference = (len(xi.shape) == 4)  # inference for model conversion
        if inference:
            xiseq = [xi]
        else:
            xiseq = xi.split(1, 0)  # t,n,c,h,w

        if self.prev_h is not None:
            self.prev_h = self.prev_h.detach()
            self.prev_c = self.prev_c.detach()
        else:
            shape = list(xiseq[0][0].shape)
            shape[1] = self.hidden_dim
            self.prev_h = torch.zeros(shape, dtype=torch.float32, device=xi.device)
            self.prev_c = torch.zeros(shape, dtype=torch.float32, device=xi.device)

        self.gate_a = []
        result = []
        for t, xt in enumerate(xiseq):
            if not inference:  # for training/val (not inference)
                xt = xt.squeeze(0)

            # 1. Make A
            if self.prev_h is not None:
                tmp = self.conv_x2a(xt) + self.conv_h2a(self.prev_h)
            else:
                tmp = self.conv_x2a(xt)

            a = tmp

            self.gate_a.append(a[None]) #store gate_a for direct supervision!

            a = torch.sigmoid(a)
            ax = a * xt

            if self.prev_h is not None:
                tmp = self.conv_h2h(self.prev_h) + self.conv_ax2h(ax)
            else:
                tmp = self.conv_ax2h(ax)

            cc_i, cc_f, cc_o, cc_g = torch.split(tmp, self.hidden_dim, dim=1)
            i = self.sigmoid(cc_i)
            f = self.sigmoid(cc_f)
            o = self.sigmoid(cc_o)
            g = self.tanh(cc_g)
            if self.prev_c is None:
                c = i * g
            else:
                c = f * self.prev_c + i * g
            h = o * self.tanh(c)
            if not inference:
                result.append(h.unsqueeze(0))
            self.prev_h = h
            self.prev_c = c
        if inference:
            res = h
        else:
            res = torch.cat(result, dim=0)
        return res

    def reset(self, mask=None):
        """To be called in between batches"""
        if mask is None or self.prev_h is None:
            self.prev_h, self.prev_c = None, None
        else:
            self.prev_h, self.prev_c = self.prev_h.detach(), self.prev_c.detach()
            mask = mask.to(self.prev_h)
            self.prev_h *= mask
            self.prev_c *= mask



class ConvGRUCell(RNNCell):
    r"""ConvGRUCell module, applies sequential part of LSTM.
    """
    def __init__(self, hidden_dim, kernel_size, bias, conv_func=nn.Conv2d, hard=False):
        super(ConvGRUCell, self).__init__(hard)
        self.hidden_dim = hidden_dim

        # Fully-Gated
        self.conv_h2zr = conv_func(in_channels=self.hidden_dim,
                                  out_channels=2 * self.hidden_dim,
                                  kernel_size=kernel_size,
                                  padding=1,
                                  bias=bias)

        self.conv_h2h = conv_func(in_channels=self.hidden_dim,
                                  out_channels=self.hidden_dim,
                                  kernel_size=kernel_size,
                                  padding=1,
                                  bias=bias)

        self.reset()

    def forward(self, xi):
        self.saturation_cost = 0

        xiseq = xi.split(1, 0) #t,n,c,h,w


        if self.prev_h is not None:
            self.prev_h = self.prev_h.detach()
        else:
            shape = list(xiseq[0].shape)
            shape[1] = self.hidden_dim
            self.prev_h = torch.zeros(shape, dtype=torch.float32, device=x.device)
            self.prev_c = torch.zeros(shape, dtype=torch.float32, device=x.device)

        result = []
        for t, xt in enumerate(xiseq):
            xt = xt.squeeze(0)


            #split x & h in 3
            x_zr, x_h = xt[:, :2*self.hidden_dim], xt[:,2*self.hidden_dim:]

            if self.prev_h is not None:
                tmp = self.conv_h2zr(self.prev_h) + x_zr
            else:
                tmp = x_zr

            cc_z, cc_r = torch.split(tmp, self.hidden_dim, dim=1)
            z = self.sigmoid(cc_z)
            r = self.sigmoid(cc_r)

            if self.prev_h is not None:
                tmp = self.conv_h2h(r * self.prev_h) + x_h
            else:
                tmp = x_h
            tmp = self.tanh(tmp)

            if self.prev_h is not None:
                h = (1-z) * self.prev_h + z * tmp
            else:
                h = z * tmp


            result.append(h.unsqueeze(0))
            self.prev_h = h
        res = torch.cat(result, dim=0)
        return res

    def reset(self):
        self.prev_h = None

class ConvnBRCCell(RNNCell):
    r"""ConvnBRCCell module, applies sequential part of nBRC cell.

        A bio-inspired bistable recurrent cell allows for
long-lasting memory
        https://arxiv.org/pdf/2006.05252.pdf
    """
    def __init__(self, hidden_dim, kernel_size, bias, conv_func=nn.Conv2d, hard=False):
        super(ConvnBRBCell, self).__init__(hard)
        self.hidden_dim = hidden_dim

        # Fully-Gated
        self.conv_h2zr = conv_func(in_channels=self.hidden_dim,
                                  out_channels=2 * self.hidden_dim,
                                  kernel_size=kernel_size,
                                  padding=1,
                                  bias=bias)

        self.conv_h2h = conv_func(in_channels=self.hidden_dim,
                                  out_channels=self.hidden_dim,
                                  kernel_size=kernel_size,
                                  padding=1,
                                  bias=bias)

        self.reset()

    def forward(self, xi):
        self.saturation_cost = 0

        xiseq = xi.split(1, 0) #t,n,c,h,w


        if self.prev_h is not None:
            self.prev_h = self.prev_h.detach()
        else:
            shape = list(xiseq[0].shape)
            shape[1] = self.hidden_dim
            self.prev_h = torch.zeros(shape, dtype=torch.float32, device=x.device)
            self.prev_c = torch.zeros(shape, dtype=torch.float32, device=x.device)

        result = []
        for t, xt in enumerate(xiseq):
            xt = xt.squeeze(0)

            #split x & h in 3
            x_zr, x_h = xt[:, :2*self.hidden_dim], xt[:,2*self.hidden_dim:]

            if self.prev_h is not None:
                tmp = self.conv_h2zr(self.prev_h) + x_zr
            else:
                tmp = x_zr

            cc_z, cc_r = torch.split(tmp, self.hidden_dim, dim=1)
            z = self.sigmoid(cc_z)
            r = self.tanh(cc_r)+1 #now R is in [0,2]

            if self.prev_h is not None:
                tmp = self.conv_h2h(r * self.prev_h) + x_h
            else:
                tmp = x_h
            tmp = self.tanh(tmp)

            if self.prev_h is not None:
                h = (1-z) * self.prev_h + z * tmp
            else:
                h = z * tmp


            result.append(h.unsqueeze(0))
            self.prev_h = h
        res = torch.cat(result, dim=0)
        return res

    def reset(self):
        self.prev_h = None


class ConvRNN(nn.Module):
    r"""ConvRNN module.
    """
    def __init__(self, in_channels, out_channels,
                 kernel_size=3, stride=1, padding=1, dilation=1,
                 cell='nBRB', hard=False, **cell_kwargs):
        super(ConvRNN, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels

        self.cell = cell

        print('rnn cell: ', cell)

        if cell == 'gru':
            self.timepool = ConvGRUCell(out_channels, 3, True, hard=hard, **cell_kwargs)
            factor = 3
        elif cell == 'nbrb':
            self.timepool = ConvnBRBCell(out_channels, 3, True, hard=hard, **cell_kwargs)
            factor = 3
        elif cell == 'alstm':
            self.timepool = ConvALSTMCell(in_channels, out_channels, 3, hard=hard, **cell_kwargs)
            factor = 1
        else:
            self.timepool = ConvLSTMCell(out_channels, 3, hard=hard, **cell_kwargs)
            factor = 4

        if cell != 'alstm':
            self.conv_x2h = SequenceWise(ConvLayer(in_channels, factor * out_channels,
                                                 kernel_size=kernel_size,
                                                 stride=stride,
                                                 dilation=dilation,
                                                 padding=padding,
                                                 activation='Identity'))


    def forward(self, x):
        #TODO: remove highway after
        if self.cell == 'alstm':
            h = self.timepool(x)
        else:
            y = self.conv_x2h(x)
            h = self.timepool(y)
        return h

    def reset(self, mask):
        self.timepool.reset(mask)



class BottleneckLSTM(RNNCell):
    # taken from https://github.com/tensorflow/models/blob/master/research/lstm_object_detection/lstm/lstm_cells.py
    def __init__(self, in_channels, out_channels, kernel_size=3, stride=1, padding=1, dilation=1, hard=False):
        super(BottleneckLSTM, self).__init__(hard)

        self.hidden_dim = out_channels//2
        self.bottleneck_x2h = SequenceWise(ConvLayer(in_channels, self.hidden_dim, kernel_size, stride, padding, dilation, separable=True))
        self.bottleneck_h2h = SeparableConv2d(self.hidden_dim, self.hidden_dim, kernel_size, 1, padding, dilation)
        self.gates = SeparableConv2d(self.hidden_dim, 4 * self.hidden_dim, 3, 1, 1)
        self.reset()
        self.stride = stride

    def forward(self, xi):
        self.saturation_cost = 0
        xi = self.bottleneck_x2h(xi)
        inference = (len(xi.shape) == 4)  # inference for model conversion
        xiseq = xi.split(1, 0)  # t,n,c,h,w

        if self.prev_h is not None:
            self.prev_h = self.prev_h.detach()
            self.prev_c = self.prev_c.detach()

        result = []
        for t, xt in enumerate(xiseq):
            if not inference:  # for training/val (not inference)
                xt = xt.squeeze(0)

            bottleneck = xt if self.prev_h is None else xt + self.bottleneck_h2h(self.prev_h)
            bottleneck = torch.tanh(bottleneck)

            gates = self.gates(bottleneck)

            cc_i, cc_f, cc_o, cc_g = torch.split(gates, self.hidden_dim, dim=1)
            i = self.sigmoid(cc_i)
            f = self.sigmoid(cc_f)
            o = self.sigmoid(cc_o)
            g = self.tanh(cc_g)
            if self.prev_c is None:
                c = i * g
            else:
                c = f * self.prev_c + i * g
            h = o * self.tanh(c)

            output = torch.cat([h, bottleneck], dim=1)
            result.append(output.unsqueeze(0))
            self.prev_h = h
            self.prev_c = c

        res = torch.cat(result, dim=0)
        return res

    def reset(self):
        self.prev_h, self.prev_c = None, None

if __name__ == '__main__':
    x = torch.rand(3, 5, 128, 16, 16)

    # sep = SeparableConv2d(128, 128, 3, 1, 1)
    # z = sep(x[0])
    # print(z.shape)

    net = ConvRNN(128, 256, 3, 1, 1, 1, cell='irnn')
    y = net(x)
    print(y.shape)
