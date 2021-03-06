from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import torch.nn as nn
import torch.nn.functional as F
from core.utils.box import box_iou
from core.utils.opts import time_to_batch, batch_to_time


def reduce(loss, mode='none'):
    if mode == 'mean':
        loss = loss.mean()
    elif mode == 'sum':
        loss = loss.sum()
    return loss


class SSDLoss(nn.Module):
    def __init__(self, num_classes, mode='focal', use_sigmoid=False, use_iou=False):
        super(SSDLoss, self).__init__()
        self.num_classes = num_classes
        self.mode = mode
        self.alpha = torch.nn.Parameter(torch.ones(num_classes))
        self.focal_loss = self._sigmoid_focal_loss if use_sigmoid else self._softmax_focal_loss
        self.use_iou = use_iou

    def _hard_negative_mining(self, cls_loss, pos):
        '''Return negative indices that is 3x the number as positive indices.

        Args:
          cls_loss: (tensor) cross entroy loss between cls_preds and cls_targets, sized [N,#anchors].
          pos: (tensor) positive class mask, sized [N,#anchors].

        Return:
          (tensor) negative indices, sized [N,#anchors].
        '''
        cls_loss = cls_loss * (pos.float() - 1)

        _, idx = cls_loss.sort(1)  # sort by negative losses
        _, rank = idx.sort(1)      # [N,#anchors]

        num_neg = 3*pos.sum(1)  # [N,]
        neg = rank < num_neg[:,None]   # [N,#anchors]
        return neg

    def _softmax_focal_loss(self, x, y, reduction='none'):
        '''Softmax Focal loss.

        Args:
          x: (tensor) predictions, sized [N,D].
          y: (tensor) targets, sized [N,].

        Return:
          (tensor) focal loss.
        '''
        gamma = 2.0
        r = torch.arange(x.size(0))
        ce = F.log_softmax(x, dim=1)[r, y]
        pt = torch.exp(ce)
        weights = (1-pt).pow(gamma)
        loss = -(weights * ce)

        return reduce(loss, reduction)

    def _sigmoid_focal_loss(self, pred, target, reduction='none'):
        '''Sigmoid Focal loss.

        Args:
          x: (tensor) predictions, sized [N,D].
          y: (tensor) targets, sized [N,].

        Return:
          (tensor) focal loss.
        '''
        alpha = 0.25
        gamma = 2.0
        pred_sigmoid = pred.sigmoid()
        target = torch.eye(self.num_classes, device=pred.device, dtype=pred.dtype)[target]

        pt = (1 - pred_sigmoid) * target + pred_sigmoid * (1 - target)
        focal_weight = (alpha * target + (1 - alpha) *
                        (1 - target)) * pt.pow(gamma)
        loss = F.binary_cross_entropy_with_logits(
            pred, target, reduction='none') * focal_weight
        loss = loss.sum(dim=-1)
        loss = reduce(loss, reduction)
        return loss

    def forward(self, loc_preds, loc_targets, cls_preds, cls_targets):
        '''Compute loss between (loc_preds, loc_targets) and (cls_preds, cls_targets).

        Args:
          loc_preds: (tensor) predicted locations, sized [N, #anchors, 4].
          loc_targets: (tensor) encoded target locations, sized [N, #anchors, 4].
          cls_preds: (tensor) predicted class confidences, sized [N, #anchors, #classes].
          cls_targets: (tensor) encoded target labels, sized [N, #anchors].

        loss:
          (tensor) loss = SmoothL1Loss(loc_preds, loc_targets) + CrossEntropyLoss(cls_preds, cls_targets).
        '''
        mask_ign = cls_targets < 0
        cls_targets[mask_ign] = 0

        pos = cls_targets > 0  # [N,#anchors]
        batch_size = pos.size(0)
        num_pos = pos.sum().item()

        #===============================================================
        # loc_loss
        #===============================================================
        if self.use_iou:
            # loc_loss = self._iou_loss(loc_preds[pos], loc_targets[pos])
            loc_loss = self._bounded_iou_loss(loc_preds[pos], loc_targets[pos])
            loc_loss = reduce(loc_loss, mode='sum')
        else:
            mask = pos.unsqueeze(2).expand_as(loc_preds)  # [N,#anchors,4]
            loc_loss = F.smooth_l1_loss(loc_preds[mask], loc_targets[mask], reduction='sum')


        #===============================================================
        # cls_loss
        #===============================================================
        if self.mode != 'focal':
            cls_loss = F.cross_entropy(cls_preds.view(-1, self.num_classes), \
                                       cls_targets.view(-1), reduction='none')  # [N*#anchors,]
            cls_loss = cls_loss.view(batch_size, -1)
            cls_loss[mask_ign] = 0  # set ignored loss to 0
            if self.mode == 'ohem':
                neg = self._hard_negative_mining(cls_loss, pos)  # [N,#anchors]
                cls_loss = cls_loss[pos|neg].sum()
            else:
                cls_loss = cls_loss.sum()
            cls_loss /= num_pos
        else:
            cls_loss = self.focal_loss(cls_preds.view(-1, self.num_classes), cls_targets.view(-1))
            cls_loss = cls_loss.view(batch_size, -1)
            cls_loss[mask_ign] = 0
            cls_loss = cls_loss.sum()
            cls_loss /= num_pos

        #print('loc_loss: %.3f | cls_loss: %.3f' % (loc_loss.item()/num_pos, cls_loss.item()/num_pos), end=' | ')
        loc_loss /= num_pos
        return loc_loss, cls_loss

    def _asso_loss(self, pred_scores, batchsize):
        """
        In Time, just promote similar pairwise scores
        :param pred_scores:
        :param target_scores:
        :return:
        """
        pred_scores_txn = batch_to_time(pred_scores, batchsize)
        loss_asso = pred_scores_txn.sum(dim=-2).std(dim=0) #sum accross anchors per class
        return loss_asso


    def _embeddings_loss(self, emb, ids, cls_targets, batchsize):
        """
        In Time, promote similarity between targets with same id

        :param pred_embeddings: [TN, #anchors, D]
        :param ids: [TN, #nanchors]
        :return:
        """
        pos = (cls_targets > 0).view(-1)
        emb = emb.view(-1, emb.size(-1))[pos]

        ids = batch_to_time(ids, batchsize)
        offsets = 100 * torch.arange(batchsize)[None,:,None].to(ids.device)
        ids = ids + offsets
        ids = time_to_batch(ids)[0]
        ids = ids.view(-1)[pos]



        #TODO:
        #1. do this per frame not globally (benchmark this)
        #2. weight id with iou loss = iou * id * (1-cos_mat) + (1-id) * max(0, cos_mat - margin)
        # We do cos(x1, x2) between every vectors
        # When they have same id = we use loss as 1 - cos (penalize for being far)
        # When they have not same id = max(0, cos - margin) (penalize for not being close)
        y = (ids[:, None] == ids[None, :])
        norms = torch.norm(emb, dim=1)
        norm_matrix = norms[:,None] * norms[None, :]
        dot_matrix = torch.mm(emb, emb.t())
        cos_matrix = dot_matrix / norm_matrix

        margin = 0.5
        loss = torch.where(y, 1 - cos_matrix, F.relu(cos_matrix - margin))


        return loss.mean()


    def _iou_loss(self, pred, target):
        """
        iou loss between positive anchors & gt
        Args:
            pred (tensor): Predicted bboxes.
            target (tensor): Target bboxes.
        """
        ious = box_iou(pred, target).max(dim=-1)[0]
        loc_loss = -torch.log(ious)
        return loc_loss

    def _bounded_iou_loss(self, pred, target, beta=0.2, eps=1e-3):
        """Improving Object Localization with Fitness NMS and Bounded IoU Loss,
        https://arxiv.org/abs/1711.00164.
        Args:
            pred (tensor): Predicted bboxes.
            target (tensor): Target bboxes.
            beta (float): beta parameter in smoothl1.
            eps (float): eps to avoid NaN.
        """
        pred_ctrx = (pred[:, 0] + pred[:, 2]) * 0.5
        pred_ctry = (pred[:, 1] + pred[:, 3]) * 0.5
        pred_w = pred[:, 2] - pred[:, 0] + 1
        pred_h = pred[:, 3] - pred[:, 1] + 1
        with torch.no_grad():
            target_ctrx = (target[:, 0] + target[:, 2]) * 0.5
            target_ctry = (target[:, 1] + target[:, 3]) * 0.5
            target_w = target[:, 2] - target[:, 0] + 1
            target_h = target[:, 3] - target[:, 1] + 1

        dx = target_ctrx - pred_ctrx
        dy = target_ctry - pred_ctry

        loss_dx = 1 - torch.max(
            (target_w - 2 * dx.abs()) /
            (target_w + 2 * dx.abs() + eps), torch.zeros_like(dx))
        loss_dy = 1 - torch.max(
            (target_h - 2 * dy.abs()) /
            (target_h + 2 * dy.abs() + eps), torch.zeros_like(dy))
        loss_dw = 1 - torch.min(target_w / (pred_w + eps), pred_w /
                                (target_w + eps))
        loss_dh = 1 - torch.min(target_h / (pred_h + eps), pred_h /
                                (target_h + eps))
        loss_comb = torch.stack([loss_dx, loss_dy, loss_dw, loss_dh],
                                dim=-1).view(loss_dx.size(0), -1)

        loss = torch.where(loss_comb < beta, 0.5 * loss_comb * loss_comb / beta,
                           loss_comb - 0.5 * beta)
        return loss
