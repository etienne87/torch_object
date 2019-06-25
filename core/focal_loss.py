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
    def __init__(self, num_classes):
        super(FocalLoss, self).__init__()
        self.num_classes = num_classes
        self.softmax = False

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
        alpha = 0.25
        gamma = 3
        t = one_hot_embedding(y, self.num_classes)
        p = x.sigmoid()
        pt = torch.where(t>0, p, 1-p)    # pt = p if t > 0 else 1-p
        w = (1-pt).pow(gamma)
        w = torch.where(t>0, alpha*w, (1-alpha)*w)
        loss = F.binary_cross_entropy_with_logits(x, t, w, size_average=False)
        return loss

    def softmax_focal_loss(self, x, y):
        '''Softmax Focal loss.

        This is described in the original paper.
        With BCELoss, the background should not be counted in num_classes.

        Args:
          x: (tensor) predictions, sized [N,D].
          y: (tensor) targets, sized [N,].

        Return:
          (tensor) focal loss.
        '''
        alpha = 0.25
        gamma = 3
        pt = F.softmax(x)[:,y]
        w = (1-pt).pow(gamma)
        p = -F.log_softmax(x, dim=1)[torch.arange(100),y].sum(dim=0)
        loss = w * p
        return loss.sum(dim=0)


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
        loc_loss = F.smooth_l1_loss(loc_preds[mask], loc_targets[mask], size_average=False)

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

        #normalization could be optional?
        loc_loss /= num_pos
        cls_loss /= num_pos
        return loc_loss, cls_loss