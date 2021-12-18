from functools import partial
from typing import Union

import torch
from torch import nn
import torch.nn.functional as F
from torch.optim.lr_scheduler import CosineAnnealingLR, LinearLR, SequentialLR
from torch.utils.data import DataLoader
from torchvision.datasets import ImageFolder
import torchvision.transforms as T
from torchvision.transforms.functional import InterpolationMode
import pytorch_lightning as pl

import backbones
from backbones.base import BaseBackbone
from extras import RandomCutMixMixUp

# https://github.com/pytorch/vision/blob/main/references/classification/train.py
# https://pytorch.org/blog/how-to-train-state-of-the-art-models-using-torchvision-latest-primitives/

_optimizers = {
    "SGD": partial(torch.optim.SGD, momentum=0.9),
    "Adam": torch.optim.Adam,
    "AdamW": torch.optim.AdamW,
    "RMSprop": partial(torch.optim.RMSprop, momentum=0.9)
}

class ImageClassifier(pl.LightningModule):
    def __init__(
        self,
        # model
        backbone: Union[str, BaseBackbone],
        num_classes: int,
        
        # data
        train_dir: str,
        val_dir: str,
        batch_size: int=128,
        num_workers: int=4,
        train_crop_size: int=176,
        val_resize_size: int=232,
        val_crop_size: int=224,
        
        # augmentation
        random_erasing_p: float=0.1,
        mixup_alpha: float=0.2,
        cutmix_alpha: float=1.0,
        
        # optimizer and scheduler
        optimizer: str="SGD",
        lr: float=0.05,
        weight_decay: float=2e-5,
        norm_weight_decay: float=0,
        label_smoothing: float=0.1,
        warmup_epochs: int=5,
        warmup_decay: float=0.01
        ):
        super().__init__()
        backbone = backbones.__dict__[backbone]() if isinstance(backbone, str) else backbone
        classifier = nn.Sequential(
            nn.AdaptiveAvgPool2d((1,1)),
            nn.Flatten(),
            nn.Linear(backbone.get_out_channels()[-1], num_classes)
        )
        self.backbone = torch.jit.script(backbone)
        self.classifier = torch.jit.script(classifier)

        train_transforms = [
            T.RandomHorizontalFlip(),
            T.autoaugment.TrivialAugmentWide(interpolation=InterpolationMode.BILINEAR),
            T.ConvertImageDtype(torch.float),
            T.Normalize(mean=(0.5,0.5,0.5), std=(0.5,0.5,0.5)),
        ]
        if random_erasing_p > 0:
            train_transforms.append(T.RandomErasing(p=random_erasing_p, value="random"))
        
        train_transforms = nn.Sequential(*train_transforms)
        self.train_transforms = torch.jit.script(train_transforms)

        mixup_cutmix = RandomCutMixMixUp(num_classes, cutmix_alpha, mixup_alpha) if cutmix_alpha > 0 and mixup_alpha > 0 else None
        self.mixup_cutmix = torch.jit.script(mixup_cutmix) if mixup_cutmix is not None else None

        self.save_hyperparameters()

    def train_dataloader(self):        
        transform = T.Compose([
            T.RandomResizedCrop(self.hparams.train_crop_size),
            T.PILToTensor()
        ])
        ds = ImageFolder(self.hparams.train_dir, transform=transform)
        dataloader = DataLoader(ds, batch_size=self.hparams.batch_size, shuffle=True, num_workers=self.hparams.num_workers, pin_memory=True)
        return dataloader

    def val_dataloader(self):
        transform = T.Compose([
            T.Resize(self.hparams.val_resize_size),
            T.CenterCrop(self.hparams.val_crop_size),
            T.PILToTensor(),
            T.ConvertImageDtype(torch.float),
            T.Normalize(mean=(0.5,0.5,0.5), std=(0.5,0.5,0.5))
        ])
        ds = ImageFolder(self.hparams.val_dir, transform=transform)
        dataloader = DataLoader(ds, batch_size=self.hparams.batch_size, shuffle=False, num_workers=self.hparams.num_workers, pin_memory=True)
        return dataloader

    def forward(self, x):
        out = self.backbone(x)
        out = self.classifier(out)
        return out

    def training_step(self, batch, batch_idx):
        images, labels = batch
        images = self.train_transforms(images)
        
        if self.mixup_cutmix is not None:
            images, labels = self.mixup_cutmix(images, labels)

        logits = self(images)
        loss = F.cross_entropy(logits, labels)
        self.log("train/loss", loss)
        
        return loss

    def validation_step(self, batch, batch_idx):
        images, labels = batch

        logits = self(images)
        loss = F.cross_entropy(logits, labels, label_smoothing=self.hparams.label_smoothing)
        self.log("val/loss", loss)

        preds = torch.argmax(logits, dim=-1)
        correct = (labels == preds).sum()
        acc = correct / labels.numel()
        self.log("val/acc", acc)

    def configure_optimizers(self):
        if self.hparams.norm_weight_decay is not None:
            # https://github.com/pytorch/vision/blob/main/torchvision/ops/_utils.py
            norm_classes = (nn.modules.batchnorm._BatchNorm, nn.LayerNorm, nn.GroupNorm)
            
            norm_params = []
            other_params = []
            for module in self.modules():
                if next(module.children(), None):
                    other_params.extend(p for p in module.parameters(recurse=False) if p.requires_grad)
                elif isinstance(module, norm_classes):
                    norm_params.extend(p for p in module.parameters() if p.requires_grad)
                else:
                    other_params.extend(p for p in module.parameters() if p.requires_grad)

            param_groups = (norm_params, other_params)
            wd_groups = (self.hparams.norm_weight_decay, self.hparams.weight_decay)
            parameters = [{"params": p, "weight_decay": w} for p, w in zip(param_groups, wd_groups) if p]

        else:
            parameters = self.parameters()

        optimizer = _optimizers[self.hparams.optimizer](parameters, lr=self.hparams.lr, weight_decay=self.hparams.weight_decay)
        
        lr_scheduler = CosineAnnealingLR(optimizer, T_max=self.trainer.max_epochs-self.hparams.warmup_epochs)
        if self.hparams.warmup_epochs > 0:
            warmup_scheduler = LinearLR(optimizer, start_factor=self.hparams.warmup_decay, total_iters=self.hparams.warmup_epochs)
            lr_scheduler = SequentialLR(optimizer, schedulers=[lr_scheduler, warmup_scheduler], milestones=[self.hparams.warmup_epochs])
            
            # https://github.com/pytorch/pytorch/issues/67318
            if not hasattr(lr_scheduler, "optimizer"):
                setattr(lr_scheduler, "optimizer", optimizer)

        return {
            "optimizer": optimizer, 
            "lr_scheduler": lr_scheduler
        }
