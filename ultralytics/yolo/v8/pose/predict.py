# Ultralytics YOLO 🚀, GPL-3.0 license

from ultralytics.yolo.engine.results import Results
from ultralytics.yolo.utils import DEFAULT_CFG, ROOT, ops
from ultralytics.yolo.utils.plotting import colors, save_one_box
from ultralytics.yolo.v8.detect.predict import DetectionPredictor


class PosePredictor(DetectionPredictor):

    def postprocess(self, preds, img, orig_img):
        preds = ops.non_max_suppression(preds,
                                        self.args.conf,
                                        self.args.iou,
                                        agnostic=self.args.agnostic_nms,
                                        max_det=self.args.max_det,
                                        classes=self.args.classes,
                                        nc=len(self.model.names))

        results = []
        for i, pred in enumerate(preds):
            orig_img = orig_img[i] if isinstance(orig_img, list) else orig_img
            shape = orig_img.shape
            pred[:, :4] = ops.scale_boxes(img.shape[2:], pred[:, :4], shape).round()
            pred_kpts = pred[:, 6:].view(len(pred), *self.model.kpt_shape) if len(pred) else pred[:, 6:]
            pred_kpts = ops.scale_coords(img.shape[2:], pred_kpts, shape)
            path, _, _, _, _ = self.batch
            img_path = path[i] if isinstance(path, list) else path
            results.append(
                Results(orig_img=orig_img,
                        path=img_path,
                        names=self.model.names,
                        boxes=pred[:, :6],
                        keypoints=pred_kpts))
        return results

    def write_results(self, idx, results, batch):
        p, im, im0 = batch
        log_string = ''
        if len(im.shape) == 3:
            im = im[None]  # expand for batch dim
        self.seen += 1
        imc = im0.copy() if self.args.save_crop else im0
        if self.source_type.webcam or self.source_type.from_img:  # batch_size >= 1
            log_string += f'{idx}: '
            frame = self.dataset.count
        else:
            frame = getattr(self.dataset, 'frame', 0)
        self.data_path = p
        self.txt_path = str(self.save_dir / 'labels' / p.stem) + ('' if self.dataset.mode == 'image' else f'_{frame}')
        log_string += '%gx%g ' % im.shape[2:]  # print string
        self.annotator = self.get_annotator(im0)

        det = results[idx].boxes  # TODO: make boxes inherit from tensors
        if len(det) == 0:
            return f'{log_string}(no detections), '
        for c in det.cls.unique():
            n = (det.cls == c).sum()  # detections per class
            log_string += f"{n} {self.model.names[int(c)]}{'s' * (n > 1)}, "

        for k in reversed(results[idx].keypoints):
            self.annotator.kpts(k, shape=results[idx].orig_shape)

        # write
        for d in reversed(det):
            cls, conf = d.cls.squeeze(), d.conf.squeeze()
            if self.args.save_txt:  # Write to file
                line = (cls, *(d.xywhn.view(-1).tolist()), conf) \
                    if self.args.save_conf else (cls, *(d.xywhn.view(-1).tolist()))  # label format
                with open(f'{self.txt_path}.txt', 'a') as f:
                    f.write(('%g ' * len(line)).rstrip() % line + '\n')
            if self.args.save or self.args.save_crop or self.args.show:  # Add bbox to image
                c = int(cls)  # integer class
                name = f'id:{int(d.id.item())} {self.model.names[c]}' if d.id is not None else self.model.names[c]
                label = None if self.args.hide_labels else (name if self.args.hide_conf else f'{name} {conf:.2f}')
                self.annotator.box_label(d.xyxy.squeeze(), label, color=colors(c, True))
            if self.args.save_crop:
                save_one_box(d.xyxy,
                             imc,
                             file=self.save_dir / 'crops' / self.model.model.names[c] / f'{self.data_path.stem}.jpg',
                             BGR=True)

        return log_string


def predict(cfg=DEFAULT_CFG, use_python=False):
    model = cfg.model or 'yolov8n-pose.pt'
    source = cfg.source if cfg.source is not None else ROOT / 'assets' if (ROOT / 'assets').exists() \
        else 'https://ultralytics.com/images/bus.jpg'

    args = dict(model=model, source=source)
    if use_python:
        from ultralytics import YOLO
        YOLO(model)(**args)
    else:
        predictor = PosePredictor(overrides=args)
        predictor.predict_cli()


if __name__ == '__main__':
    predict()