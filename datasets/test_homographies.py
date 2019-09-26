from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import numpy as np
import cv2
from pycocotools.coco import COCO



def moving_average(item, alpha):
    return (1-alpha)*item + alpha * np.random.randn(*item.shape)


def computeC2MC1(R_0to1, tvec_0to1, R_0to2, tvec_0to2):
    R_1to2 = R_0to2.dot(R_0to1.T)
    tvec_1to2 = R_0to2.dot(-R_0to1.T.dot(tvec_0to1)) + tvec_0to2
    return R_1to2, tvec_1to2

def generate_homography(rvec1, tvec1, rvec2, tvec2, nt, K, Kinv, d):
    R_0to1 = cv2.Rodrigues(rvec1)[0].transpose()
    tvec_0to1 = np.dot(-R_0to1, tvec1.reshape(3, 1))

    R_0to2 = cv2.Rodrigues(rvec2)[0].transpose()
    tvec_0to2 = np.dot(-R_0to2, tvec2.reshape(3, 1))

    #view 0to2
    nt1 = R_0to1.dot(nt.T).reshape(1, 3)
    H_0to2 = R_0to2 - np.dot(tvec_0to2.reshape(3, 1), nt1) / d
    G_0to2 = np.dot(K, np.dot(H_0to2, Kinv))


    #view 1to2
    # R_1to2, tvec_1to2 = computeC2MC1(R_0to1, tvec_0to1, R_0to2, tvec_0to2)
    # H_1to2 = R_1to2 - np.dot(tvec_1to2.reshape(3, 1), nt1) / d
    # G_1to2 = np.dot(K, np.dot(H_1to2, Kinv))
    return G_0to2


def viz_diff(diff):
    diff = diff.clip(diff.mean() - 3 * diff.std(), diff.mean() + 3 * diff.std())
    diff = (diff - diff.min()) / (diff.max() - diff.min())
    return diff


class PlanarVoyage(object):
    def __init__(self, height, width):
        self.K = np.array([[width / 2, 0, width / 2],
                      [0, width / 2, height / 2],
                      [0, 0, 1]], dtype=np.float32)
        self.Kinv = np.linalg.inv(self.K)

        self.rvec1 = np.array([0, 0, 0], dtype=np.float32)
        self.tvec1 = np.array([0, 0, 0], dtype=np.float32)
        self.nt = np.array([0, 0, -1], dtype=np.float32).reshape(1, 3)

        self.rvec_speed = np.random.rand(3) * 0.25
        self.tvec_speed = np.random.rand(3) * 0.5
        self.tshift = np.random.randn(3)
        self.d = 1
        self.time = 0

    def __call__(self):
        tshift = moving_average(self.tshift, 1e-4)
        rvec2 = self.rvec_speed * np.sin(self.time * 0.01)
        tvec2 = self.tvec_speed * np.sin(self.time * 0.01 + tshift)
        G_0to2 = generate_homography(self.rvec1, self.tvec1, rvec2, tvec2, self.nt, self.K, self.Kinv, self.d)

        self.time += 1
        return G_0to2


def show_voyage(img, anns):
    height, width = img.shape[:2]

    voyage = PlanarVoyage(height, width)

    prev_img = img.astype(np.float32)


    mask = coco.annToMask(anns[0])
    for i in range(len(anns)):
        mask += coco.annToMask(anns[i])
    mask_rgb = cv2.applyColorMap((mask * 30) % 255, cv2.COLORMAP_HSV) * (mask > 0)[:, :, None].repeat(3, 2)
    mask_rgb = mask_rgb.astype(np.float32)/255.0


    d = 1
    t = 0

    while 1:

        G_0to2 = voyage()

        out = cv2.warpPerspective(img, G_0to2, (width, height),
                                  flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT).astype(np.float32)
        out = (out - out.min()) / (out.max() - out.min())


        out_mask = cv2.warpPerspective(mask_rgb, G_0to2, (width, height),
                                  flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT).astype(np.float32)

        diff = out - prev_img

        # Remove some events
        diff *= np.random.rand(height, width, 3) < 0.9


        diff = viz_diff(diff) + out_mask / 3

        print(diff.min(), diff.max())

        # Salt-and-Pepper
        # diff += (np.random.rand(height, width)[:,:,None].repeat(3,2) < 0.00001)/2
        # diff -= (np.random.rand(height, width)[:,:,None].repeat(3,2) < 0.00001) / 2

        cv2.imshow("diff", diff)
        cv2.imshow("out", out)
        key = cv2.waitKey(5)
        if key == 27:
            break
        prev_img = out

        t += 1


if __name__ == '__main__':
    import glob
    #imgs = glob.glob("/home/etienneperot/workspace/data/coco/images/train2017/"+"*.jpg")

    dataDir = '/home/etienneperot/workspace/data/coco'
    dataType = 'val2017'
    annFile = '{}/annotations/instances_{}.json'.format(dataDir, dataType)
    coco = COCO(annFile)

    catIds = coco.getCatIds(catNms=['person', 'car'])
    imgIds = coco.getImgIds(catIds=catIds)

    while 1:
        img = coco.loadImgs(imgIds[np.random.randint(0, len(imgIds))])[0]
        file_name = os.path.join(dataDir, 'images', dataType, img['file_name'])
        annIds = coco.getAnnIds(imgIds=img['id'], catIds=catIds, iscrowd=None)
        anns = coco.loadAnns(annIds)
        image = cv2.imread(file_name)
        mask = coco.annToMask(anns[0])
        for i in range(len(anns)):
            mask += coco.annToMask(anns[i])


        show_voyage(image, anns)
