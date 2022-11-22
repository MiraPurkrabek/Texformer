#!/usr/bin/env bash

# IN__DATA="/datagrid/personal/purkrmir/data/floorball_human_mesh_test/manual/all/occlusion_E_00.png"
IN__DATA="/datagrid/personal/purkrmir/data/floorball_human_mesh_test/manual/all/"
OUT_DATA="/datagrid/personal/purkrmir/data/floorball_human_mesh_test/manual/all/Texformer_tmp/"
SEG_DATA=""



if [ "$SEG_DATA" == "" ]; then
    python \
        demo.py \
        --img_path $IN__DATA \
        --save_path $OUT_DATA 

else
    python \
        demo.py \
        --img_path $IN__DATA \
        --save_path $OUT_DATA \
        --seg_path $SEG_DATA 

fi

