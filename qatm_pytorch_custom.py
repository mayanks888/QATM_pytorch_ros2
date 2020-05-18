import time
import numpy as np
import cv2
import matplotlib.pyplot as plt
from pathlib import Path
from seaborn import color_palette
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
from torchvision import models, transforms, utils
import copy
from utils import *
# %matplotlib inline
# from color_function import color_Detect
# # CONVERT IMAGE TO TENSOR

class ImageDataset(torch.utils.data.Dataset):
    def __init__(self, template_dir_path, image_name, thresh_csv=None, transform=None):
        self.transform = transform
        if not self.transform:
            self.transform = transforms.Compose([
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225],
                )
            ])
        self.template_path = list(template_dir_path.iterdir())
        self.image_name = image_name
        
        self.image_raw = cv2.imread(self.image_name)
        # self.image_raw=cv2.resize(self.image_raw,(int(self.image_raw.shape[1]/2),int(self.image_raw.shape[0]/2)))

        
        self.thresh_df = None
        if thresh_csv:
            self.thresh_df = pd.read_csv(thresh_csv)
            
        if self.transform:
            self.image = self.transform(self.image_raw).unsqueeze(0)
        
    def __len__(self):
        return len(self.template_names)
    
    def __getitem__(self, idx):
        template_path = str(self.template_path[idx])
        template = cv2.imread(template_path)
        # template=cv2.resize(template,(int(template.shape[1]/2),int(template.shape[0]/2)))

        if self.transform:
            template = self.transform(template)
        # thresh = 0.7
        thresh = .99
        if self.thresh_df is not None:
            if self.thresh_df.path.isin([template_path]).sum() > 0:
                thresh = float(self.thresh_df[self.thresh_df.path==template_path].thresh)
        return {'image': self.image, 
                    'image_raw': self.image_raw, 
                    'image_name': self.image_name,
                    'template': template.unsqueeze(0), 
                    'template_name': template_path, 
                    'template_h': template.size()[-2],
                   'template_w': template.size()[-1],
                   'thresh': thresh}


# template_dir = 'template/'
# image_path = 'sample/sample1.jpg'
# dataset = ImageDataset(Path(template_dir), image_path, thresh_csv='thresh_template.csv')
#

# ### EXTRACT FEATURE

class Featex():
    def __init__(self, model, use_cuda):
        self.use_cuda = use_cuda
        self.feature1 = None
        self.feature2 = None
        self.model= copy.deepcopy(model.eval())
        self.model = self.model[:17]
        for param in self.model.parameters():
            param.requires_grad = False
        if self.use_cuda:
            self.model = self.model.cuda()
        self.model[2].register_forward_hook(self.save_feature1)
        self.model[16].register_forward_hook(self.save_feature2)

    #this is just like the feature pyramid function used to get more spacial information
    def save_feature1(self, module, input, output):
        self.feature1 = output.detach()
    
    def save_feature2(self, module, input, output):
        self.feature2 = output.detach()
        
    def __call__(self, input, mode='big'):
        if self.use_cuda:
            input = input.cuda()
            #model run here(forward function implemented here)
        _ = self.model(input)
        if mode=='big':
            # resize feature1 to the same size of feature2
            self.feature1 = F.interpolate(self.feature1, size=(self.feature2.size()[2], self.feature2.size()[3]), mode='bilinear', align_corners=True)
        else:        
            # resize feature2 to the same size of feature1
            self.feature2 = F.interpolate(self.feature2, size=(self.feature1.size()[2], self.feature1.size()[3]), mode='bilinear', align_corners=True)
        return torch.cat((self.feature1, self.feature2), dim=1)


class MyNormLayer():
    def __call__(self, x1, x2):
        bs, _ , H, W = x1.size()
        _, _, h, w = x2.size()
        x1 = x1.view(bs, -1, H*W)
        x2 = x2.view(bs, -1, h*w)
        concat = torch.cat((x1, x2), dim=2)
        x_mean = torch.mean(concat, dim=2, keepdim=True)
        x_std = torch.std(concat, dim=2, keepdim=True)
        x1 = (x1 - x_mean) / x_std
        x2 = (x2 - x_mean) / x_std
        x1 = x1.view(bs, -1, H, W)
        x2 = x2.view(bs, -1, h, w)
        return [x1, x2]


class CreateModel():
    def __init__(self, alpha, model, use_cuda):
        self.alpha = alpha
        self.featex = Featex(model, use_cuda)
        self.I_feat = None
        self.I_feat_name = None
    def __call__(self, template, image, image_name):
        t1 = time.time()
        T_feat = self.featex(template)
        print('total time taken template image  feature extractor ', (time.time() - t1) * 1000)
        if self.I_feat_name is not image_name:
            t1 = time.time()
            self.I_feat = self.featex(image)
            self.I_feat_name = image_name
            print('total time taken feature sample image extractor ', (time.time() - t1) * 1000)
        conf_maps = None
        batchsize_T = T_feat.size()[0]
        for i in range(batchsize_T):
            T_feat_i = T_feat[i].unsqueeze(0)
            #this is where normilisation was done on the image feature as well as template feature.
            # t1=time.time()
            t1=time.time()
            I_feat_norm, T_feat_i = MyNormLayer()(self.I_feat, T_feat_i)
            print('total time take normalisation ', (time.time() - t1) * 1000)
            t1 = time.time()
            #this one is to unite the two matrix together in the given way
            dist = torch.einsum("xcab,xcde->xabde", I_feat_norm / torch.norm(I_feat_norm, dim=1, keepdim=True), T_feat_i / torch.norm(T_feat_i, dim=1, keepdim=True))
            print('total time take einsum ', (time.time() - t1) * 1000)
            t1 = time.time()
            conf_map = QATM(self.alpha)(dist)
            print('total time take qatm ', (time.time() - t1) * 1000)
            if conf_maps is None:
                conf_maps = conf_map
            else:
                conf_maps = torch.cat([conf_maps, conf_map], dim=0)
        return conf_maps


class QATM():
    def __init__(self, alpha):
        self.alpha = alpha

    def __call__(self, x):
        batch_size, ref_row, ref_col, qry_row, qry_col = x.size()
        x = x.view(batch_size, ref_row*ref_col, qry_row*qry_col)
        #substracting row with max value in ref column and same we will do with querry column
        #
        xm_ref = x - torch.max(x, dim=1, keepdim=True)[0]
        xm_qry = x - torch.max(x, dim=2, keepdim=True)[0]
        # cosine similarity function
        t7 = time.time()
        confidence = torch.sqrt(F.softmax(self.alpha*xm_ref, dim=1) * F.softmax(self.alpha * xm_qry, dim=2))
        print('total time take softmax inside qatm', (time.time() - t7) * 1000)
        #top tk is will give the top value in each row with index
        t8 = time.time()
        conf_values, ind3 = torch.topk(confidence, 1)
        print('total time take topk inside qatm', (time.time() - t8) * 1000)
        ind1, ind2 = torch.meshgrid(torch.arange(batch_size), torch.arange(ref_row*ref_col))
        ind1 = ind1.flatten()
        ind2 = ind2.flatten()
        ind3 = ind3.flatten()
        if x.is_cuda:
            ind1 = ind1.cuda()
            ind2 = ind2.cuda()
        #this is to filter the confidence with highest value of querry values
        values = confidence[ind1, ind2, ind3]
        values = torch.reshape(values, [batch_size, ref_row, ref_col, 1])
        return values
    def compute_output_shape( self, input_shape ):
        bs, H, W, _, _ = input_shape
        return (bs, H, W, 1)


# # NMS AND PLOT

# ## SINGLE

def nms(score, w_ini, h_ini, thresh=0.7):
    score=score.squeeze()
    dots = np.array(np.where(score > thresh*score.max()))

    x1 = dots[1] - w_ini//2
    x2 = x1 + w_ini
    y1 = dots[0] - h_ini//2
    y2 = y1 + h_ini

    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    scores = score[dots[0], dots[1]]
    # scores=score[0][dots[1], dots[2]]
    order = scores.argsort()[::-1]

    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)

        inds = np.where(ovr <= 0.5)[0]
        order = order[inds + 1]
    boxes = np.array([[x1[keep], y1[keep]], [x2[keep], y2[keep]]]).transpose(2, 0, 1)
    return boxes


def plot_result(image_raw, boxes, show=False, save_name=None, color=(255, 0, 0)):
    # plot result
    d_img = image_raw.copy()
    for box in boxes:
        d_img = cv2.rectangle(d_img, tuple(box[0]), tuple(box[1]), color, 3)
    if show:
        plt.imshow(d_img)
    if save_name:
        cv2.imwrite(save_name, d_img[:,:,::-1])
    return d_img

def plot_result_mayank(image_raw, boxes, show=False, save_name=None, color=(255, 0, 0)):
    # plot result
    d_img = image_raw.copy()
    for box in boxes:
        d_img = cv2.rectangle(d_img, tuple(box[0]), tuple(box[1]), color, 3)
    if show:
        # plt.imshow(d_img)
        cv2.imshow("img", d_img)
        # cv2.imshow('img', img)
        ch = cv2.waitKey(2)
        if ch & 0XFF == ord('q'):
            cv2.destroyAllWindows()
        # cv2.waitKey(1)
        cv2.destroyAllWindows()
    if save_name:
        cv2.imwrite(save_name, d_img[:,:,::-1])
    return d_img

# ## MULTI

def nms_multi(scores, w_array, h_array, thresh_list):
    indices = np.arange(scores.shape[0])
    maxes = np.max(scores.reshape(scores.shape[0], -1), axis=1)
    # omit not-matching templates
    scores_omit = scores[maxes > 0.1 * maxes.max()]
    indices_omit = indices[maxes > 0.1 * maxes.max()]
    # extract candidate pixels from scores
    dots = None
    dos_indices = None
    for index, score in zip(indices_omit, scores_omit):
        #here is filtering og score is happening is based on the threshold value*max score value in result matrix
        dot = np.array(np.where(score > thresh_list[index]*score.max()))
        if dots is None:
            dots = dot
            dots_indices = np.ones(dot.shape[-1]) * index
        else:
            dots = np.concatenate([dots, dot], axis=1)
            dots_indices = np.concatenate([dots_indices, np.ones(dot.shape[-1]) * index], axis=0)
    dots_indices = dots_indices.astype(np.int)
    x1 = dots[1] - w_array[dots_indices]//2
    x2 = x1 + w_array[dots_indices]
    y1 = dots[0] - h_array[dots_indices]//2
    y2 = y1 + h_array[dots_indices]

    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    scores = scores[dots_indices, dots[0], dots[1]]
    order = scores.argsort()[::-1]
    dots_indices = dots_indices[order]
    
    keep = []
    keep_index = []
    while order.size > 0:
        i = order[0]
        index = dots_indices[0]
        keep.append(i)
        keep_index.append(index)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)

        inds = np.where(ovr <= 0.05)[0]
        order = order[inds + 1]
        dots_indices = dots_indices[inds + 1]
        
    boxes = np.array([[x1[keep], y1[keep]], [x2[keep], y2[keep]]]).transpose(2,0,1)
    return boxes, np.array(keep_index)


def plot_result_multi(image_raw, boxes, indices, show=False, save_name=None, color_list=None):
    d_img = image_raw.copy()
    if color_list is None:
        color_list = color_palette("hls", indices.max()+1)
        color_list = list(map(lambda x: (int(x[0]*255), int(x[1]*255), int(x[2]*255)), color_list))
    for i in range(len(indices)):
        d_img = plot_result(d_img, boxes[i][None, :,:].copy(), color=color_list[indices[i]])
        # if i>1:
        #     break
        # break

        #################3
        # color_frame
        bbox_info = boxes[i][None, :, :].copy()
        xmin=bbox_info[0, 0][0]
        xmax=bbox_info[0, 1][0]
        ymin=bbox_info[0,0][1]
        ymax=bbox_info[0,1][1]


        # frame = d_img[int(bbox_info[1]):int(bbox_info[3]), int(bbox_info[0]):int(bbox_info[2])]
        frame = d_img[int(ymin):int(ymax), int(xmin):int(xmax)]
        # frame = img[int(bbox_info[1]-15):int(bbox_info[3]+15), int(bbox_info[0]-15):int(bbox_info[2]+15)]
        # frame = img[int(y[value][1]):int(y[value][3]), int(y[value][0]):int(y[value][2])]
        # frame = img[int(y[value][1]-40):int(y[value][3]+40), int(y[value][0]-30):int(y[value][2]+30)]
        cv2.imwrite("data/cust_template/myimage_outPut.jpg", frame)
        # light_col=color_Detect()
        # cv2.putText(d_img, str(light_col), (10, 10), cv2.FONT_HERSHEY_SIMPLEX, .50, (0, 255, 0),
        #             lineType=cv2.LINE_AA)
        # print(" time taken in template processing", (time.time() - t1) * 1000)
        #################
    if show:
        # plt.imshow(d_img)
        cv2.imshow("img",d_img)
        # cv2.imshow('img', img)
        ch = cv2.waitKey(0)
        if ch & 0XFF == ord('q'):
            cv2.destroyAllWindows()
        # cv2.waitKey(1)
        cv2.destroyAllWindows()

    if save_name:
        cv2.imwrite(save_name, d_img[:,:,::-1])
    return d_img


# # RUNNING

def run_one_sample(model, template, image, image_name):
    val = model(template, image, image_name)
    if val.is_cuda:
        val = val.cpu()
    val = val.numpy()
    val = np.log(val)
    
    batch_size = val.shape[0]
    scores = []
    for i in range(batch_size):
        # compute geometry average on score map
        gray = val[i,:,:,0]
        gray = cv2.resize( gray, (image.size()[-1], image.size()[-2]) )

        ###########################33
        # # plt.imshow(d_img)
        # cv2.imshow("imge", gray)
        # # cv2.imshow('img', img)
        # ch = cv2.waitKey(10000)
        # if ch & 0XFF == ord('q'):
        #     cv2.destroyAllWindows()
        # # cv2.waitKey(1)
        # cv2.destroyAllWindows()
        # 3
        #############################
        h = template.size()[-2]
        w = template.size()[-1]
        score = compute_score( gray, w, h) 
        score[score>-1e-7] = score.min()
        score = np.exp(score / (h*w)) # reverse number range back after computing geometry average
        scores.append(score)
    return np.array(scores)


def run_one_sample_mayank(model, dataset):
    for data in dataset:
        # score = run_one_sample(model, data['template'], data['image'], data['image_name'])
        template=data['template']
        image=  data['image']
        image_name= data['image_name']
        w_array=(data['template_w'])
        h_array=(data['template_h'])
        thresh_list=(data['thresh'])
    val = model(template, image, image_name)
    if val.is_cuda:
        val = val.cpu()
    val = val.numpy()
    val = np.log(val)

    batch_size = val.shape[0]
    scores = []
    for i in range(batch_size):
        # compute geometry average on score map
        gray = val[i, :, :, 0]
        gray = cv2.resize(gray, (image.size()[-1], image.size()[-2]))

        ###########################33
        # # plt.imshow(d_img)
        # cv2.imshow("imge", gray)
        # # cv2.imshow('img', img)
        # ch = cv2.waitKey(10000)
        # if ch & 0XFF == ord('q'):
        #     cv2.destroyAllWindows()
        # # cv2.waitKey(1)
        # cv2.destroyAllWindows()
        # 3
        #############################
        h = template.size()[-2]
        w = template.size()[-1]
        score = compute_score(gray, w, h)
        score[score > -1e-7] = score.min()
        score = np.exp(score / (h * w))  # reverse number range back after computing geometry average
        scores.append(score)
    return np.array(scores),np.array(w_array), np.array(h_array), thresh_list


def run_multi_sample(model, dataset):
    scores = None
    w_array = []
    h_array = []
    thresh_list = []
    for data in dataset:
        score = run_one_sample(model, data['template'], data['image'], data['image_name'])
        if scores is None:
            scores = score
        else:
            scores = np.concatenate([scores, score], axis=0)
        w_array.append(data['template_w'])
        h_array.append(data['template_h'])
        thresh_list.append(data['thresh'])
    return np.array(scores), np.array(w_array), np.array(h_array), thresh_list
        # break

def run_multi_sample_univ(model, dataset):
    scores = None
    w_array = []
    h_array = []
    thresh_list = []
    for data in dataset:
        score = run_one_sample(model, data['template'], data['image'], data['image_name'])
        if scores is None:
            scores = score
        else:
            scores = np.concatenate([scores, score], axis=0)
        w_array.append(data['template_w'])
        h_array.append(data['template_h'])
        thresh_list.append(data['thresh'])
    return np.array(scores), np.array(w_array), np.array(h_array), thresh_list


if __name__ == '__main__':
    template_dir = 'template/'
    image_path = 'sample/sample1.jpg'
    dataset = ImageDataset(Path(template_dir), image_path, thresh_csv='thresh_template.csv')

    model = CreateModel(model=models.vgg19(pretrained=True).features, alpha=25, use_cuda=True)
    #

    ##################################3333

    # resnet = models.resnet18(pretrained=True)
    # modules = list(resnet.children())[:-1]  # delete the last fc layer.
    # resnet = nn.Sequential(*modules)
    # ### Now set requires_grad to false
    # for param in resnet.parameters():
    #     param.requires_grad = False
    # model = CreateModel(model=resnet, alpha=25, use_cuda=True)
    ########################################

    scores, w_array, h_array, thresh_list = run_multi_sample(model, dataset)

    boxes, indices = nms_multi(scores, w_array, h_array, thresh_list)

    d_img = plot_result_multi(dataset.image_raw, boxes, indices, show=True, save_name='result_sample.png')

    plt.imshow(scores[2])


