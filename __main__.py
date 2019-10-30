#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import tifffile as tif
from tifffile import TiffWriter
import gc
import re
import numpy as np
import pandas as pd
from datetime import datetime
import os
import cv2 as cv 

from ome_tags import create_ome_metadata, get_channel_metadata
from adaptive_estimation import AdaptiveShiftEstimation
from image_positions import load_necessary_xml_tags, get_image_sizes_auto, get_image_sizes_manual, get_image_paths_for_fields_per_channel, get_image_paths_for_planes_per_channel
from image_processing import stitch_z_projection, create_z_projection_for_fov, stitch_series_of_planes, stitch_plane2


def main():

    parser = argparse.ArgumentParser(
        description="Phenix image stitcher.\nPLEASE DO NOT USE SINGLE QUOTES FOR ARGS")
    parser.add_argument('--xml', type=str, required=True,
                        help='path to the xml file typically ../Images/Index.idx.xml')
    parser.add_argument('--img_dir', type=str, required=True,
                        help='path to the directory with images.')
    parser.add_argument('--out_dir', type=str, required=True,
                        help='path to output directory.')
    parser.add_argument('--reference_channel', type=str, default='none',
                        help='select channel that will be used for estimating stitching parameters. Default is to use first channel.')
    parser.add_argument('--make_preview', action='store_true', default=False,
                        help='will generate z-max projection of reference channel (typically first channel).')
    parser.add_argument('--stitch_channels', type=str, nargs='+', default=['all'], 
                        help='specify space separated channel names to stitch (e.g. "DAPI" "ALEXA 657"); \nall: will stitch all channels. Default to stitch all channels.')
    parser.add_argument('--channels_to_correct_illumination', type=str, nargs='+', default=['all'], 
                        help='specify space separated channel names that require correction of bad illumination (e.g. "DAPI"), RNA spot channels usually do not need correction.\nall: will apply correction to all channels. \nnone: will not apply to any.')
    parser.add_argument('--mode', type=str, default='regular_channel', 
                        help='regular_channel: produce z-stacks, save by channel.\nregular_plane: produce z-stacks, save by plane.\nmaxz: produce max z-projections instead of z-stacks.')
    parser.add_argument('--adaptive', action='store_true',
                        help='turn on adaptive estimation of image translation')
    parser.add_argument('--overlap', type=float, nargs='+', default=[0.1, 0.1],
                        help='two values that correspond to horizontal and vertical overlap of images in fractions of 1. Default overalp: horizontal 0.1, vertical 0.1.')
    parser.add_argument('--save_params', action='store_true', default=False,
                        help='will save parameters estimated during stitching into 3 csv files (image_ids, x_sizes, y sizes)')
    parser.add_argument('--load_params', type=str, default='none',
                        help='specify folder that contais the following csv files: image_ids.csv, x_size.csv, y_sizes.csv, that contain previously estimated parameters')
    parser.add_argument('--scan_mode', type=str, default='none',
                        help='specify scanning mode (auto or manual)')
    args = parser.parse_args()

    xml_path = args.xml
    img_dir = args.img_dir
    out_dir = args.out_dir
    reference_channel = args.reference_channel
    make_preview = args.make_preview
    stitch_only_ch = args.stitch_channels
    ill_cor_ch = args.channels_to_correct_illumination
    stitching_mode = args.mode
    is_adaptive = args.adaptive
    overlap = args.overlap
    save_params = args.save_params
    load_params = args.load_params
    scan_mode = args.scan_mode

    # check if specified directories exist
    if not os.path.isdir(img_dir):
        raise ValueError('img_dir does not exist')
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    
    if not out_dir.endswith('/'):
        out_dir = out_dir + '/'
    if not img_dir.endswith('/'):
        img_dir = img_dir + '/'
    
    st = datetime.now()
    print('\nstarted', st)

    '''
    xml_path = 'C:/Users/vv3/Desktop/image/images/Hiplex_run1_cycle1_MsPos__2019-03-05T10_52_04-Measurement_2/Index.idx.xml'
    img_dir = 'C:/Users/vv3/Desktop/image/images/Hiplex_run1_cycle1_MsPos__2019-03-05T10_52_04-Measurement_2/Images/'
    out_dir = 'C:/Users/vv3/Desktop/image/stitched/'
    reference_channel = 'DAPI'
    '''
    
    tag_Images, tag_Name, tag_MeasurementStartTime = load_necessary_xml_tags(xml_path)
    fields_path_list = get_image_paths_for_fields_per_channel(img_dir, tag_Images)
    planes_path_list = get_image_paths_for_planes_per_channel(img_dir, tag_Images)
    nchannels = len(planes_path_list.keys())
    channel_names = list(planes_path_list.keys())
    channel_ids = {ch: i for i, ch in enumerate(channel_names)}

    if stitch_only_ch == ['all']:
        if reference_channel == 'none':
            reference_channel = channel_names[0]
    elif stitch_only_ch != ['all']:
        # if user specified custom number of channels check if they are correct
        for i in stitch_only_ch:
            if i not in channel_names:
                raise ValueError('There is no channel with name ' + i + ' in the XML file')
        if reference_channel == 'none':
            reference_channel = stitch_only_ch[0]
        nchannels = len(stitch_only_ch)
        channel_names = stitch_only_ch

    channel_ids = {k: v for k, v in channel_ids.items() if k in channel_names}

    if ill_cor_ch == ['all']:
        ill_cor_ch = channel_names
    elif ill_cor_ch == ['none']:
        ill_cor_ch = []

    if load_params == 'none':
        if scan_mode == 'auto':
            img_sizes = get_image_sizes_auto(tag_Images, reference_channel)
            parameters = img_sizes
            ids = []
            x_size = []
            y_size = []
            for row in img_sizes:
                ids.append([i[2] for i in row])
                x_size.append([i[0] for i in row])
                y_size.append(row[0][1])
        elif scan_mode == 'manual':
            ids, x_size, y_size = get_image_sizes_manual(tag_Images, reference_channel)
            parameters = ids

        if is_adaptive:
            print('estimating image translation')
            z_max_img_list = create_z_projection_for_fov(reference_channel, fields_path_list)
            estimator = AdaptiveShiftEstimation()
            estimator.horizontal_overlap_percent = overlap[0]
            estimator.vertical_overlap_percent = overlap[1]
            x_size, y_size = estimator.estimate(z_max_img_list, parameters, scan_mode)
            del z_max_img_list
    else:
        print('using parameters from loaded files')
        
        if not load_params.endswith('/'):
            load_params = load_params + '/'

        if scan_mode == 'auto':
            ids = []
            with open(load_params + 'image_ids.txt') as f:
                for line in f.readlines():
                    line = re.sub("[\n\s']+", '', line).split(',')
                    line = [int(i) if i != 'zeros' else i for i in line]
                    ids.append(line)
            x_size = []
            with open(load_params + 'x_sizes.txt') as f:
                for line in f.readlines():
                    line = re.sub("[\n\s']+", '', line).split(',')
                    line = [int(i) if i != 'zeros' else i for i in line]
                    x_size.append(line)
            y_size = []
            with open(load_params + 'y_sizes.txt') as f:
                line = f.read()
                line = re.sub("[\n\s']+", '', line).split(',')
                line = [int(i) if i != 'zeros' else i for i in line]
                y_size = line
        elif scan_mode == 'manual':
            ids = pd.read_csv(load_params + 'image_ids.csv', index_col=0, header='infer', dtype='object')
            x_size = pd.read_csv(load_params + 'x_sizes.csv', index_col=0, header='infer', dtype='int64')
            y_size = pd.read_csv(load_params + 'y_sizes.csv', index_col=0, header='infer', dtype='int64')
            # convert column names to int
            ids.columns = ids.columns.astype(int)
            x_size.columns = x_size.columns.astype(int)
            y_size.columns = y_size.columns.astype(int)
            # convert data to int where possible
            for j in ids.columns:
                for i in ids.index:
                    try:
                        val = ids.loc[i, j]
                        val = int(val)
                        ids.loc[i, j] = val
                    except ValueError:
                        pass

    if save_params:
        print('saving parameters')
        if scan_mode == 'auto':
            with open(out_dir + 'image_ids.txt', 'w') as f:
                for row in ids:
                    f.write(','.join(str(i) for i in row) + '\n')
            with open(out_dir + 'x_sizes.txt', 'w') as f:
                for row in x_size:
                    f.write(','.join(str(i) for i in row) + '\n')
            with open(out_dir + 'y_sizes.txt', 'w') as f:
                f.write(','.join(str(i) for i in y_size) + '\n')
        elif scan_mode == 'manual':
            ids.to_csv(out_dir + 'image_ids.csv')
            x_size.to_csv(out_dir + 'x_sizes.csv')
            y_size.to_csv(out_dir + 'y_sizes.csv')

    if scan_mode == 'auto':
        width = sum(x_size[0])
        height = sum(y_size)
    elif scan_mode == 'manual':
        width = sum(x_size.iloc[0, :])
        height = sum(y_size.iloc[:, 0])
    nplanes = len(planes_path_list[reference_channel])

    channels_meta = get_channel_metadata(tag_Images, channel_ids)
    final_meta = dict()
    for i, channel in enumerate(channel_names):
        final_meta[channel] = channels_meta[channel].replace('Channel', 'Channel ID="Channel:0:' + str(i) + '"')
    ome = create_ome_metadata(tag_Name, 'XYCZT', width, height, nchannels, nplanes, 1, 'uint16', final_meta, tag_Images, tag_MeasurementStartTime)
    ome_maxz = create_ome_metadata(tag_Name, 'XYCZT', width, height, nchannels, 1, 1, 'uint16', final_meta, tag_Images, tag_MeasurementStartTime)

    if make_preview:
        print('generating max z preview')
        z_proj = stitch_z_projection(reference_channel, fields_path_list, ids, x_size, y_size, False, scan_mode)
        preview_meta = {reference_channel: final_meta[reference_channel]}
        ome_preview = create_ome_metadata(tag_Name, 'XYCZT', width, height, 1, 1, 1,
                                          'uint16', preview_meta, tag_Images, tag_MeasurementStartTime)
        tif.imwrite(out_dir + 'preview.tif', z_proj, description=ome_preview)
        print('preview is available at ' + out_dir + 'preview.tif')
        del z_proj
        gc.collect()

    if stitching_mode == 'regular_channel':
        final_path_reg = out_dir + tag_Name + '.tif'
        with TiffWriter(final_path_reg, bigtiff=True) as TW:
            for i, channel in enumerate(channel_names):
                print('\nprocessing channel no.{0}/{1} {2}'.format(i+1, nchannels, channel))
                print('started at', datetime.now())
                
                if channel in ill_cor_ch:
                    do_illum_cor = True
                else:
                    do_illum_cor = False
                               
                TW.save(stitch_series_of_planes(channel, planes_path_list, ids, x_size, y_size, do_illum_cor, scan_mode), photometric='minisblack', contiguous=True, description=ome)
    
    elif stitching_mode == 'regular_plane':
        final_path_reg = out_dir + tag_Name + '.tif'
        delete = '\b'*20
        contrast_limit = 127
        grid_size = (41, 41)
        clahe = cv.createCLAHE(contrast_limit, grid_size)
        with TiffWriter(final_path_reg, bigtiff=True) as TW:
            for i, channel in enumerate(channel_names):
                print('\nprocessing channel no.{0}/{1} {2}'.format(i+1, nchannels, channel))
                print('started at', datetime.now())
                if channel in ill_cor_ch:
                    do_illum_cor = True
                else:
                    do_illum_cor = False
                    
                for j, plane in enumerate(planes_path_list[channel]):
                    print('{0}plane {1}/{2}'.format(delete, j+1, nplanes), end='', flush=True)
                    TW.save(stitch_plane2(plane, clahe, ids, x_size, y_size, do_illum_cor, scan_mode), photometric='minisblack', contiguous=True, description=ome)
                
    elif stitching_mode == 'maxz':
        final_path_maxz = out_dir + 'maxz_' + tag_Name + '.tif'
        with TiffWriter(final_path_maxz, bigtiff=True) as TW:
            for i, channel in enumerate(channel_names):
                print('\nprocessing channel no.{0}/{1} {2}'.format(i+1, nchannels, channel))
                print('started at', datetime.now())
                
                if channel in ill_cor_ch:
                    do_illum_cor = True
                else:
                    do_illum_cor = False
                
                TW.save(stitch_z_projection(channel, fields_path_list, ids, x_size, y_size, do_illum_cor, scan_mode), photometric='minisblack',contiguous=True, description=ome_maxz)

    with open(out_dir + 'ome_meta.xml', 'w', encoding='utf-8') as f:
        if stitching_mode == 'regular_plane' or stitching_mode == 'regular_channel':
            f.write(ome)
        if stitching_mode == 'maxz':
            f.write(ome_maxz)

    fin = datetime.now()
    print('\nelapsed time', fin-st)


if __name__ == '__main__':
    main()
