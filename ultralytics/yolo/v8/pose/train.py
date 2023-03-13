# Ultralytics YOLO 🚀, GPL-3.0 license

from copy import copy

import torch
import torch.nn as nn

from ultralytics.nn.tasks import PoseModel
from ultralytics.yolo import v8
from ultralytics.yolo.utils import DEFAULT_CFG
from ultralytics.yolo.utils.loss import KeypointLoss
from ultralytics.yolo.utils.ops import xyxy2xywh
from ultralytics.yolo.utils.plotting import plot_images, plot_results
from ultralytics.yolo.utils.tal import make_anchors
from ultralytics.yolo.utils.torch_utils import de_parallel
from ultralytics.yolo.v8.detect.train import Loss


# BaseTrainer python usage
class PoseTrainer(v8.detect.DetectionTrainer):

    def __init__(self, cfg=DEFAULT_CFG, overrides=None):
        if overrides is None:
            overrides = {}
        overrides['task'] = 'pose'
        super().__init__(cfg, overrides)

    def get_model(self, cfg=None, weights=None, verbose=True):
        model = PoseModel(cfg, ch=3, nc=self.data['nc'], nkpt=self.data['nkpt'], verbose=verbose)
        if weights:
            model.load(weights)

        return model

    def get_validator(self):
        self.loss_names = 'box_loss', 'pose_loss', 'kobj_loss', 'cls_loss', 'dfl_loss'
        return v8.pose.PoseValidator(self.test_loader, save_dir=self.save_dir, args=copy(self.args))

    def criterion(self, preds, batch):
        if not hasattr(self, 'compute_loss'):
            self.compute_loss = PoseLoss(de_parallel(self.model))
        return self.compute_loss(preds, batch)

    def plot_training_samples(self, batch, ni):
        images = batch['img']
        kpts = batch['keypoints']
        cls = batch['cls'].squeeze(-1)
        bboxes = batch['bboxes']
        paths = batch['im_file']
        batch_idx = batch['batch_idx']
        plot_images(images,
                    batch_idx,
                    cls,
                    bboxes,
                    kpts=kpts,
                    paths=paths,
                    fname=self.save_dir / f'train_batch{ni}.jpg')

    def plot_metrics(self):
        plot_results(file=self.csv, pose=True)  # save results.png


# Criterion class for computing training losses
class PoseLoss(Loss):

    def __init__(self, model):  # model must be de-paralleled
        super().__init__(model)
        self.nkpt = model.model[-1].nkpt  # number of keypoints
        self.bce_pose = nn.BCEWithLogitsLoss()
        self.keypoint_loss = KeypointLoss(device=self.device, nkpt=self.nkpt)

    def __call__(self, preds, batch):
        loss = torch.zeros(5, device=self.device)  # box, cls, dfl, kpt_location, kpt_visibility
        feats, pred_kpts = preds if isinstance(preds[0], list) else preds[1]
        pred_distri, pred_scores = torch.cat([xi.view(feats[0].shape[0], self.no, -1) for xi in feats], 2).split(
            (self.reg_max * 4, self.nc), 1)

        # b, grids, ..
        pred_scores = pred_scores.permute(0, 2, 1).contiguous()
        pred_distri = pred_distri.permute(0, 2, 1).contiguous()
        pred_kpts = pred_kpts.permute(0, 2, 1).contiguous()

        dtype = pred_scores.dtype
        imgsz = torch.tensor(feats[0].shape[2:], device=self.device, dtype=dtype) * self.stride[0]  # image size (h,w)
        anchor_points, stride_tensor = make_anchors(feats, self.stride, 0.5)

        # targets
        batch_size = pred_scores.shape[0]
        batch_idx = batch['batch_idx'].view(-1, 1)
        targets = torch.cat((batch_idx, batch['cls'].view(-1, 1), batch['bboxes']), 1)
        targets = self.preprocess(targets.to(self.device), batch_size, scale_tensor=imgsz[[1, 0, 1, 0]])
        gt_labels, gt_bboxes = targets.split((1, 4), 2)  # cls, xyxy
        mask_gt = gt_bboxes.sum(2, keepdim=True).gt_(0)

        # pboxes
        pred_bboxes = self.bbox_decode(anchor_points, pred_distri)  # xyxy, (b, h*w, 4)
        # pred_kpts = self.kpts_decode(anchor_points, pred_kpts)  # (b, h*w, 51)

        _, target_bboxes, target_scores, fg_mask, target_gt_idx = self.assigner(
            pred_scores.detach().sigmoid(), (pred_bboxes.detach() * stride_tensor).type(gt_bboxes.dtype),
            anchor_points * stride_tensor, gt_labels, gt_bboxes, mask_gt)

        target_scores_sum = max(target_scores.sum(), 1)

        # cls loss
        # loss[1] = self.varifocal_loss(pred_scores, target_scores, target_labels) / target_scores_sum  # VFL way
        loss[3] = self.bce(pred_scores, target_scores.to(dtype)).sum() / target_scores_sum  # BCE

        # bbox loss
        if fg_mask.sum():
            target_bboxes /= stride_tensor
            loss[0], loss[4] = self.bbox_loss(pred_distri, pred_bboxes, anchor_points, target_bboxes, target_scores,
                                              target_scores_sum, fg_mask)
            keypoints = batch['keypoints'].to(self.device).float().clone()
            keypoints[:, 0::3] *= imgsz[1]
            keypoints[:, 1::3] *= imgsz[0]
            for i in range(batch_size):
                if fg_mask[i].sum():
                    idx = target_gt_idx[i][fg_mask[i]]
                    gt_kpt = keypoints[batch_idx.view(-1) == i][idx]  # (n, 51)
                    gt_kpt[:, 0::3] /= stride_tensor[fg_mask[i]]
                    gt_kpt[:, 1::3] /= stride_tensor[fg_mask[i]]
                    xywh = xyxy2xywh(target_bboxes[i][fg_mask[i]])
                    area = xywh[:, 2:].prod(1, keepdim=True)
                    # pred_kpt = pred_kpts[i][fg_mask[i]]
                    pred_kpt = self.kpts_decode(anchor_points[fg_mask[i]], pred_kpts[i][fg_mask[i]], xywh)
                    kpt_mask = gt_kpt[:, 2::3] != 0
                    loss[1] += self.keypoint_loss(pred_kpt, gt_kpt, kpt_mask, area)
                    # kpt_score loss
                    loss[2] += self.bce_pose(pred_kpt[:, 2::3], kpt_mask.float())

        # WARNING: Uncomment lines below in case of Multi-GPU DDP unused gradient errors
        #         else:
        #             loss[1] += proto.sum() * 0
        # else:
        #     loss[1] += proto.sum() * 0

        loss[0] *= self.hyp.box  # box gain
        loss[1] *= self.hyp.box / batch_size  # pose gain
        loss[2] *= self.hyp.box / batch_size  # TODO: pose_score gain
        loss[3] *= self.hyp.cls  # cls gain
        loss[4] *= self.hyp.dfl  # dfl gain

        return loss.sum() * batch_size, loss.detach()  # loss(box, cls, dfl)

    def kpts_decode(self, anchor_points, pred_kpts, bbox):
        # TODO
        y = pred_kpts.clone()
        y[..., 0::3] *= 2
        y[..., 1::3] *= 2
        y[..., 0::3] += anchor_points[:, [0]] - 0.5
        y[..., 1::3] += anchor_points[:, [1]] - 0.5
        # y[:, 0::3] = (y[:, 0::3].sigmoid() - 0.5) * bbox[:, [2]] + anchor_points[:, [0]]
        # y[:, 1::3] = (y[:, 1::3].sigmoid() - 0.5) * bbox[:, [3]] + anchor_points[:, [1]]
        # y[:, 0::3] = (y[:, 0::3].sigmoid() - 0.5) * bbox[:, [2]] + bbox[:, [0]]
        # y[:, 1::3] = (y[:, 1::3].sigmoid() - 0.5) * bbox[:, [3]] + bbox[:, [1]]
        return y


def train(cfg=DEFAULT_CFG, use_python=False):
    model = cfg.model or 'yolov8n-pose.yaml'
    data = cfg.data or 'coco128-kpt.yaml'  # or yolo.ClassificationDataset("mnist")
    device = cfg.device if cfg.device is not None else ''
    cfg.batch = 1  # Temp
    args = dict(model=model, data=data, device=device)
    if use_python:
        from ultralytics import YOLO
        YOLO(model).train(**args)
    else:
        trainer = PoseTrainer(overrides=args)
        trainer.train()


if __name__ == '__main__':
    train()