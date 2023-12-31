from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import os
import sys
import h5py
import math
import time
import torch
import random
import numpy as np
from PIL import Image
from glob import glob
from torch.utils.data import Dataset
from torch.utils.data import DataLoader

import sys
sys.path.append('/braindat/lab/chenyd/code/Miccai23')
from utils.augmentation import SimpleAugment
from utils.consistency_aug_perturbations import Rescale
from utils.consistency_aug_perturbations import Filp
from utils.consistency_aug_perturbations import Intensity
from utils.consistency_aug_perturbations import GaussBlur
from utils.consistency_aug_perturbations import GaussNoise
from utils.consistency_aug_perturbations import Cutout
from utils.consistency_aug_perturbations import SobelFilter
from utils.consistency_aug_perturbations import Mixup
from utils.consistency_aug_perturbations import Elastic
from utils.consistency_aug_perturbations import Artifact
from utils.consistency_aug_perturbations import Missing
from utils.consistency_aug_perturbations import BlurEnhanced


class Train(Dataset):
	def __init__(self, cfg):
		super(Train, self).__init__()
		self.cfg = cfg
		self.model_type = cfg.MODEL.model_type
		self.per_mode = cfg.DATA.per_mode

		# basic settings
		# the input size of network
		if cfg.MODEL.model_type == 'superhuman':
			self.crop_size = [18, 160, 160]
			self.net_padding = [0, 0, 0]
		elif cfg.MODEL.model_type == 'mala':
			self.crop_size = [53, 268, 268]
			self.net_padding = [14, 106, 106]  # the edge size of patch reduced by network
		elif cfg.MODEL.model_type == 'UNETR':
			self.crop_size = [32, 160, 160]
			self.net_padding = [0, 0, 0]
		else:
			raise AttributeError('No this model type!')

		# the output size of network
		# for mala: [25, 56, 56]
		# for superhuman: [18, 160, 160]
		self.out_size = [self.crop_size[k] - 2 * self.net_padding[k] for k in range(len(self.crop_size))]

		# training dataset files (h5), may contain many datasets
		self.folder_name = cfg.DATA.folder_name
		self.father_path = glob('/braindat/lab/chenyd/DATASET/miccai_pretrain_data/' + '*EM*')

		# split training data
		# self.train_split = cfg.DATA.train_split
		# self.unlabel_split = cfg.DATA.unlabel_split

		# augmentation
		self.simple_aug = SimpleAugment()
		self.if_norm_images = cfg.DATA.if_norm_images
		self.if_scale_aug = True
		self.scale_factor = cfg.DATA.scale_factor
		self.if_filp_aug = True
		self.if_rotation_aug = False
		self.if_intensity_aug = cfg.DATA.if_intensity_aug_unlabel
		self.if_elastic_aug = cfg.DATA.if_elastic_aug_unlabel
		self.if_noise_aug = cfg.DATA.if_noise_aug_unlabel
		self.min_noise_std = cfg.DATA.min_noise_std
		self.max_noise_std = cfg.DATA.max_noise_std
		self.if_mask_aug = cfg.DATA.if_mask_aug_unlabel
		self.if_blur_aug = cfg.DATA.if_blur_aug_unlabel
		self.min_kernel_size = cfg.DATA.min_kernel_size
		self.max_kernel_size = cfg.DATA.max_kernel_size
		self.min_sigma = cfg.DATA.min_sigma
		self.max_sigma = cfg.DATA.max_sigma
		self.if_sobel_aug = cfg.DATA.if_sobel_aug_unlabel
		self.if_mixup_aug = cfg.DATA.if_mixup_aug_unlabel
		self.if_misalign_aug = cfg.DATA.if_misalign_aug_unlabel
		self.if_artifact_aug = cfg.DATA.if_artifact_aug_unlabel
		self.if_missing_aug = cfg.DATA.if_missing_aug_unlabel
		self.if_blurenhanced_aug = cfg.DATA.if_blurenhanced_aug_unlabel

		self.train_datasets = []
		for i in self.father_path:
			self.train_datasets += glob(i + '/*.hdf')
		self.data_len = len(self.train_datasets)
		self.unlabel_split_rate = cfg.DATA.unlabel_split_rate
		# load dataset

		# padding by 'reflect' mode for mala network
		# if cfg.MODEL.model_type == 'mala':
		# 	for k in range(len(self.dataset)):
		# 		self.dataset[k] = np.pad(self.dataset[k], ((self.net_padding[0], self.net_padding[0]), \
		# 												   (self.net_padding[1], self.net_padding[1]), \
		# 												   (self.net_padding[2], self.net_padding[2])), mode='reflect')

		# # the training dataset size
		# self.raw_data_shape = list(self.dataset[0].shape)

		# padding for augmentation
		self.sub_padding = [0, 80, 80]  # for rescale
		# self.sub_padding = [0, 0, 0]
		self.crop_from_origin = [self.crop_size[i] + 2*self.sub_padding[i] for i in range(len(self.sub_padding))]

		# perturbations
		self.perturbations_init()

	def __getitem__(self, index):
		# random select one dataset if contain many datasets
		k = random.randint(0, len(self.train_datasets)-1)
		try:
			f_raw = h5py.File(self.train_datasets[k], 'r')
			data = f_raw['main'][:]
		except:
			k = random.randint(0, len(self.train_datasets)-1)
			f_raw = h5py.File(self.train_datasets[k], 'r')
			data = f_raw['main'][:]

		f_raw.close()
		self.unlabel_split = int(data.shape[0] * self.unlabel_split_rate)
		
		if data.shape[0] > self.unlabel_split:
			# The default is to use the bottom 100 sections of volumes as unlabeled data for simplicity
			# However, you can use any other sections (except from labeled sections) as unlabeled data according to your needs
			data = data[:self.unlabel_split]
		# self.dataset.append(data)

		if self.cfg.MODEL.model_type == 'mala':
			data = np.pad(data, ((self.net_padding[0], self.net_padding[0]), \
														   (self.net_padding[1], self.net_padding[1]), \
														   (self.net_padding[2], self.net_padding[2])), mode='reflect')

		# the training dataset size
		self.raw_data_shape = list(data.shape)
		used_data = data

		# random select one sub-volume
		random_z = random.randint(0, self.raw_data_shape[0]-self.crop_from_origin[0])
		random_y = random.randint(0, self.raw_data_shape[1]-self.crop_from_origin[1])
		random_x = random.randint(0, self.raw_data_shape[2]-self.crop_from_origin[2])
		imgs = used_data[random_z:random_z+self.crop_from_origin[0], \
						random_y:random_y+self.crop_from_origin[1], \
						random_x:random_x+self.crop_from_origin[2]].copy()

		# do augmentation
		imgs = imgs.astype(np.float32) / 255.0
		[imgs] = self.simple_aug([imgs])
		gt_imgs = imgs[:, self.sub_padding[-1]:-self.sub_padding[-1], self.sub_padding[-1]:-self.sub_padding[-1]]
		imgs1, _, _, _ = self.apply_perturbations(imgs, None, mode=self.per_mode)
		imgs2, _, _, _ = self.apply_perturbations(imgs, None, mode=self.per_mode)

		# extend dimension
		imgs1 = imgs1[np.newaxis, ...]
		imgs2 = imgs2[np.newaxis, ...]
		gt_imgs = gt_imgs[np.newaxis, ...]
		imgs1 = np.ascontiguousarray(imgs1, dtype=np.float32)
		imgs2 = np.ascontiguousarray(imgs2, dtype=np.float32)
		gt_imgs = np.ascontiguousarray(gt_imgs, dtype=np.float32)
		return imgs1, imgs2, gt_imgs

	def perturbations_init(self):
		self.per_rescale = Rescale(scale_factor=self.scale_factor, det_shape=self.crop_size)
		self.per_flip = Filp()
		self.per_intensity = Intensity()
		self.per_gaussnoise = GaussNoise(min_std=self.min_noise_std, max_std=self.max_noise_std, norm_mode='trunc')
		self.per_gaussblur = GaussBlur(min_kernel=self.min_kernel_size, max_kernel=self.max_kernel_size, min_sigma=self.min_sigma, max_sigma=self.max_sigma)
		self.per_cutout = Cutout(model_type=self.model_type)
		self.per_sobel = SobelFilter(if_mean=True)
		self.per_mixup = Mixup(min_alpha=0.1, max_alpha=0.4)
		self.per_misalign = Elastic(control_point_spacing=[4, 40, 40], jitter_sigma=[0, 0, 0], prob_slip=0.2, prob_shift=0.2, max_misalign=17, padding=20)
		self.per_elastic = Elastic(control_point_spacing=[4, 40, 40], jitter_sigma=[0, 2, 2], padding=20)
		self.per_artifact = Artifact(min_sec=1, max_sec=5)
		self.per_missing = Missing(miss_fully_ratio=0.2, miss_part_ratio=0.5)
		self.per_blurenhanced = BlurEnhanced(blur_fully_ratio=0.5, blur_part_ratio=0.7)

	def apply_perturbations(self, data, auxi=None, mode=1):
		all_pers = [self.if_scale_aug, self.if_filp_aug, self.if_rotation_aug, self.if_intensity_aug, \
					self.if_noise_aug, self.if_blur_aug, self.if_mask_aug, self.if_sobel_aug, \
					self.if_mixup_aug, self.if_misalign_aug, self.if_elastic_aug, self.if_artifact_aug, \
					self.if_missing_aug, self.if_blurenhanced_aug]
		if mode == 1:
			# select used perturbations
			used_pers = []
			for k, value in enumerate(all_pers):
				if value:
					used_pers.append(k)
			# select which one perturbation to use
			if len(used_pers) == 0:
				# do nothing
				# must crop
				data = data[:, self.sub_padding[-1]:-self.sub_padding[-1], self.sub_padding[-1]:-self.sub_padding[-1]]
				scale_size = data.shape[-1]
				rule = np.asarray([0,0,0,0], dtype=np.int32)
				rotnum = 0
				return data, scale_size, rule, rotnum
			elif len(used_pers) == 1:
				# No choise if only one perturbation can be used
				rand_per = used_pers[0]
			else:
				rand_per = random.choice(used_pers)
			# do augmentation
			# resize
			if rand_per == 0:
				data, scale_size = self.per_rescale(data)
			else:
				data = data[:, self.sub_padding[-1]:-self.sub_padding[-1], self.sub_padding[-1]:-self.sub_padding[-1]]
				scale_size = data.shape[-1]
			# flip
			if rand_per == 1:
				data, rule = self.per_flip(data)
			else:
				rule = np.asarray([0,0,0,0], dtype=np.int32)
			# rotation
			if rand_per == 2:
				rotnum = random.randint(0, 3)
				data = np.rot90(data, k=rotnum, axes=(1,2))
			else:
				rotnum = 0
			# intensity
			if rand_per == 3:
				data = self.per_intensity(data)
			# noise
			if rand_per == 4:
				data = self.per_gaussnoise(data)
			# blur
			if rand_per == 5:
				data = self.per_gaussblur(data)
			# mask or cutout
			if rand_per == 6:
				data = self.per_cutout(data)
			# sobel
			if rand_per == 7:
				data = self.per_sobel(data)
			# mixup
			if rand_per == 8 and auxi is not None:
				data = self.per_mixup(data, auxi)
			# misalign
			if rand_per == 9:
				data = self.per_misalign(data)
			# elastic
			if rand_per == 10:
				data = self.per_elastic(data)
			# artifact
			if rand_per == 11:
				data = self.per_artifact(data)
			# missing section
			if rand_per == 12:
				data = self.per_missing(data)
			# blur enhanced
			if rand_per == 13:
				data = self.per_blurenhanced(data)
		else:
			raise NotImplementedError
		return data, scale_size, rule, rotnum

	def __len__(self):
		return int(sys.maxsize)


class Provider(object):
	def __init__(self, stage, cfg):
			#patch_size, batch_size, num_workers, is_cuda=True):
		self.stage = stage
		if self.stage == 'train':
			self.data = Train(cfg)
			self.batch_size = cfg.TRAIN.batch_size
			self.num_workers = cfg.TRAIN.num_workers
		elif self.stage == 'valid':
			# return valid(folder_name, kwargs['data_list'])
			pass
		else:
			raise AttributeError('Stage must be train/valid')
		self.is_cuda = cfg.TRAIN.if_cuda
		self.data_iter = None
		self.iteration = 0
		self.epoch = 1
	
	def __len__(self):
		return self.data.num_per_epoch
	
	def build(self):
		if self.stage == 'train':
			self.data_iter = iter(DataLoader(dataset=self.data, batch_size=self.batch_size, num_workers=self.num_workers,
                                             shuffle=False, drop_last=False, pin_memory=True))
		else:
			self.data_iter = iter(DataLoader(dataset=self.data, batch_size=1, num_workers=0,
                                             shuffle=False, drop_last=False, pin_memory=True))
	
	def next(self):
		if self.data_iter is None:
			self.build()
		try:
			batch = self.data_iter.next()
			self.iteration += 1
			if self.is_cuda:
				batch[0] = batch[0].cuda()
				batch[1] = batch[1].cuda()
				batch[2] = batch[2].cuda()
			return batch
		except StopIteration:
			self.epoch += 1
			self.build()
			self.iteration += 1
			batch = self.data_iter.next()
			if self.is_cuda:
				batch[0] = batch[0].cuda()
				batch[1] = batch[1].cuda()
				batch[2] = batch[2].cuda()
			return batch

def show(img3d):
	# only used for image with shape [18, 160, 160]
	num = img3d.shape[0]
	column = 5
	row = math.ceil(num / float(column))
	size = img3d.shape[1]
	img_all = np.zeros((size*row, size*column), dtype=np.uint8)
	for i in range(row):
		for j in range(column):
			index = i*column + j
			if index >= num:
				img = np.zeros_like(img3d[0], dtype=np.uint8)
			else:
				img = (img3d[index] * 255).astype(np.uint8)
			img_all[i*size:(i+1)*size, j*size:(j+1)*size] = img
	return img_all


if __name__ == '__main__':
	import yaml
	from attrdict import AttrDict
	from utils.show import show_one
	""""""
	seed = 555
	np.random.seed(seed)
	random.seed(seed)
	cfg_file = 'pretraining_all.yaml'
	with open(os.path.join('/braindat/lab/chenyd/code/Miccai23/config',cfg_file), 'r') as f:
		cfg = AttrDict( yaml.safe_load(f) )
	
	out_path = os.path.join('/braindat/lab/chenyd/code/Miccai23/', 'data_temp_UNETR')
	if not os.path.exists(out_path):
		os.mkdir(out_path)
	data = Train(cfg)
	print(data.data_len)
	t = time.time()
	for i in range(0, 20):
		t1 = time.time()
		tmp_data1, tmp_data2, gt = iter(data).__next__()
		
		tmp_data1 = np.squeeze(tmp_data1)
		tmp_data2 = np.squeeze(tmp_data2)
		gt = np.squeeze(gt)
		print('tmp_data1 shape: ', tmp_data1.shape, 'tmp_data2 shape: ', tmp_data2.shape, 'gt shape: ', gt.shape)
		if cfg.MODEL.model_type == 'mala':
			tmp_data1 = tmp_data1[14:-14,106:-106,106:-106]
			tmp_data2 = tmp_data2[14:-14,106:-106,106:-106]
			gt = gt[14:-14,106:-106,106:-106]

		print('single cost time: ', time.time()-t1)
		

		img_data1 = show_one(tmp_data1)
		img_data2 = show_one(tmp_data2)
		img_affs = show_one(gt)
		im_cat = np.concatenate([img_data1, img_data2, img_affs], axis=1)

		Image.fromarray(im_cat).save(os.path.join(out_path, str(i).zfill(4)+'.png'))
	print(time.time() - t)