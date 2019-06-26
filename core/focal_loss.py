from __future__ import print_function

import torch
import torch.nn as nn
import torch.nn.functional as F


def one_hot_embedding(labels, num_classes):
    '''Embedding labels to one-hot.
    Args:
      labels: (LongTensor) class labels, sized [N,].
      num_classes: (int) number of classes.
    Returns:
      (tensor) encoded labels, sized [N,#classes].
    '''
    y = torch.eye(num_classes, device=labels.device)  # [D,D]
    return y[labels]  # [N,D]



class FocalLoss(nn.Module):
    def __init__(self, num_classes, softmax=False):
        super(FocalLoss, self).__init__()
        self.num_classes = num_classes
        self.softmax = softmax
        self.alpha = nn.Parameter(torch.ones(num_classes,).float())


    def sigmoid_focal_loss(self, x, y):
        '''Sigmoid Focal loss.

        This is described in the original paper.
        With BCELoss, the background should not be counted in num_classes.

        Args:
          x: (tensor) predictions, sized [N,D].
          y: (tensor) targets, sized [N,].

        Return:
          (tensor) focal loss.
        '''
        gamma = 2
        z = one_hot_embedding(y, self.num_classes)
        p = x.sigmoid()
        pt = torch.where(z>0, p, 1-p)    # pt = p if t > 0 else 1-p
        weights = (1-pt).pow(gamma)
        weights = torch.where(z>0, 0.25*weights, (1-0.25)*weights)
        losses = F.relu(x) - x * z + torch.log(1 + torch.exp(-torch.abs(x)))
        loss = losses * weights
        return loss.mean()

    def softmax_focal_loss(self, x, y):
        '''Softmax Focal loss.

        Args:
          x: (tensor) predictions, sized [N,D].
          y: (tensor) targets, sized [N,].

        Return:
          (tensor) focal loss.
        '''
        gamma = 2
        r = torch.arange(x.size(0))
        pt = F.softmax(x, dim=1)[r,y]
        weights = (1-pt).pow(gamma) #should normalize?
        ce = -F.log_softmax(x, dim=1)[r,y]
        loss = weights * ce * self.alpha[y]
        return loss.mean()


    def forward(self, loc_preds, loc_targets, cls_preds, cls_targets):
        '''Compute loss between (loc_preds, loc_targets) and (cls_preds, cls_targets).

        Args:
          loc_preds: (tensor) predicted locations, sized [batch_size, #anchors, 4].
          loc_targets: (tensor) encoded target locations, sized [batch_size, #anchors, 4].
          cls_preds: (tensor) predicted class confidences, sized [batch_size, #anchors, #classes].
          cls_targets: (tensor) encoded target labels, sized [batch_size, #anchors].

        loss:
          (tensor) loss = SmoothL1Loss(loc_preds, loc_targets) + FocalLoss(cls_preds, cls_targets).
        '''
        batch_size, num_boxes = cls_targets.size()
        pos = cls_targets > 0  # [N,#anchors]
        num_pos = pos.sum().item()

        #===============================================================
        # loc_loss = SmoothL1Loss(pos_loc_preds, pos_loc_targets)
        #===============================================================
        mask = pos.unsqueeze(2).expand_as(loc_preds)       # [N,#anchors,4]
        loc_loss = F.smooth_l1_loss(loc_preds[mask], loc_targets[mask], size_average=True)

        #===============================================================
        # cls_loss = FocalLoss(cls_preds, cls_targets)
        #===============================================================
        pos_neg = cls_targets > -1  # exclude ignored anchors
        mask = pos_neg.unsqueeze(2).expand_as(cls_preds)
        masked_cls_preds = cls_preds[mask].view(-1,self.num_classes)

        if self.softmax:
            cls_loss = self.softmax_focal_loss(masked_cls_preds, cls_targets[pos_neg])
        else:
            cls_loss = self.sigmoid_focal_loss(masked_cls_preds, cls_targets[pos_neg])


        return loc_loss, cls_loss