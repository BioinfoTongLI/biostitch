import os
import re
import numpy as np
import dask
import tifffile as tif
import cv2 as cv


def alphaNumOrder(string):
    """ Returns all numbers on 5 digits to let sort the string with numeric order.
    Ex: alphaNumOrder("a6b12.125")  ==> "a00006b00012.00125"
    """
    return ''.join([format(int(x), '05d') if x.isdigit()
                    else x for x in re.split(r'(\d+)', string)])


def read_images(path, is_dir):
    """ Rread images in natural order (with respect to numbers) """

    allowed_extensions = ('tif', 'tiff')

    if is_dir:
        file_list = [fn for fn in os.listdir(path) if fn.endswith(allowed_extensions)]
        file_list.sort(key=alphaNumOrder)
        task = [dask.delayed(tif.imread)(path + fn) for fn in file_list]
        img_list = dask.compute(*task, scheduler='threads')
        #img_list = list(map(tif.imread, [path + fn for fn in file_list]))
    else:
        if isinstance(path, list):
            task = [dask.delayed(tif.imread)(p) for p in path]
            img_list = dask.compute(*task, scheduler='threads')
            #img_list = list(map(tif.imread, path))
        else:
            img_list = tif.imread(path)

    return img_list


def equalize_histogram(img_list):
    """ Function for adaptive normalization of image histogram CLAHE """
    nrows, ncols = img_list[0].shape
    grid_size = [int(round(max((ncols, nrows)) / 20))] * 2
    grid_size = tuple(i if i % 2 != 0 else i + 1 for i in grid_size)
    contrast_limit = 256
    def clahe_process(img):
        clahe = cv.createCLAHE(contrast_limit, grid_size)
        return clahe.apply(img)
        
    task = [dask.delayed(clahe_process)(img) for img in img_list]
    img_list = dask.compute(*task, scheduler='processes')
    return img_list


def z_project(field):
    """ Wrapper function to support multiprocessing """
    return np.max(np.stack(read_images(field, is_dir=False), axis=0), axis=0)


def create_z_projection_for_fov(channel_name, path_list):
    """ Read images, convert them into stack, get max z-projection"""
    channel = path_list[channel_name]
    task = [dask.delayed(z_project)(field) for field in channel]
    z_max_img_list = dask.compute(*task, scheduler='threads')

    #for field in channel:
    #   z_max_img_list.append(np.max(np.stack(read_images(field, is_dir=False), axis=0), axis=0))
    return z_max_img_list


def stitch_z_projection(channel_name, fields_path_list, ids, x_size, y_size, y_pos, do_illum_cor, scan_mode):
    """ Create max z projection for each field of view """
    z_max_fov_list = create_z_projection_for_fov(channel_name, fields_path_list)
    if do_illum_cor:
        z_max_fov_list = equalize_histogram(z_max_fov_list)
        z_proj = stitch_images(z_max_fov_list, ids, x_size, y_size, y_pos, scan_mode)
    else:
        z_proj = stitch_images(z_max_fov_list, ids, x_size, y_size, y_pos, scan_mode)
    
    return z_proj


def crop_images_scan_manual(images, ids, x_sizes, y_sizes):
    """ Read data from dataframe ids, series x_sizes and y_sizes and crop images """
    x_sizes = x_sizes.to_list()
    y_sizes = y_sizes.to_list()
    ids = ids.to_list()
    default_img_shape = images[0].shape
    dtype = images[0].dtype.type
    r_images = []
    for j, _id in enumerate(ids):
        if _id == 'zeros':
            img = np.zeros((y_sizes[j], x_sizes[j]), dtype=dtype)
        else:
            x_shift = default_img_shape[1] - x_sizes[j]
            y_shift = default_img_shape[0] - y_sizes[j]

            img = images[_id][y_shift:, x_shift:]
        r_images.append(img)
    return r_images


def crop_images_scan_auto(images, ids, x_sizes, y_sizes):
    default_img_shape = images[0].shape
    dtype = images[0].dtype.type
    r_images = []

    for j, _id in enumerate(ids):
        if _id == 'zeros' and (j == 0 or j == len(ids) - 1):
            continue
        elif _id == 'zeros':
            img = np.zeros((y_sizes[j], x_sizes[j]), dtype=dtype)
        else:
            x_shift = default_img_shape[1] - x_sizes[j]
            y_shift = default_img_shape[0] - y_sizes[j]
            img = images[_id][y_shift:, x_shift:]

        r_images.append(img)

    return r_images


def stitch_images(images, ids, x_size, y_size, y_pos, scan_mode):
    """ Stitch cropped images by concatenating them horizontally and vertically """

    dtype = images[0].dtype.type
    if scan_mode == 'auto':
        big_img_width = sum(x_size[0])
        big_img_height = max(y_pos + 2160)#sum([row[0] for row in y_size])
        res = np.zeros((big_img_height, big_img_width), dtype=dtype)
        nrows = len(y_size)

        # calculate horizontal and vertical position to insert image rows into big image
        # padding values are used to calculate horizontal position
        # cumulative sum is used to calculate vertical position
        left_pad = [row[0] for row in x_size]
        right_pad = [sum(row[:-1]) for row in x_size]

        #y_pos_in_big_img = list(np.cumsum([row[0] for row in y_size]))
        #y_pos_in_big_img.insert(0, 0)

        # concatenate and insert image row
        for row in range(0, nrows):
            f = y_pos[row]  # from

            img_row = np.concatenate(crop_images_scan_auto(images, ids[row], x_size[row], y_size[row]), axis=1)
            t = f + img_row.shape[0]  # to
            res[f:t, left_pad[row]:right_pad[row]] = img_row

    elif scan_mode == 'manual':
        big_img_width = sum(x_size.iloc[0, :])
        big_img_height = sum(y_size.iloc[:, 0])
        res = np.zeros((big_img_height, big_img_width), dtype=dtype)
        nrows = ids.shape[0]

        # calculate horizontal and vertical position to insert image rows into big image
        # cumulative sum is used to calculate vertical position
        y_pos_in_big_img = list(np.cumsum(y_size.iloc[:, 0]))
        y_pos_in_big_img.insert(0, 0)

        # concatenate and insert image row
        for row in range(0, nrows):
            f = y_pos_in_big_img[row]  # from
            t = y_pos_in_big_img[row + 1]  # to
            res[f : t, :] = np.concatenate(crop_images_scan_manual(images, ids.iloc[row, :], x_size.iloc[row, :], y_size.iloc[row, :]), axis=1)
    return res


def stitch_plane(plane_paths, ids, x_size, y_size, y_pos, do_illum_cor, scan_mode):
    """ Do histogram normalization and stitch multiple images into one plane """
    images = read_images(plane_paths, is_dir=False)
    dtype = images[0].dtype.type
    ncols = sum(x_size.iloc[0, :])
    nrows = sum(y_size.iloc[:, 0])
    result_plane = np.zeros((1, nrows, ncols), dtype=dtype)
    if do_illum_cor:
        images = equalize_histogram(images)
    result_plane[0, :, :] = stitch_images(images, ids, x_size, y_size, y_pos, scan_mode)
    return result_plane
