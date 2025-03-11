# stac-3dep-1m
Static STAC Catalog + STAC GeoParquet for 3DEP 1m DEMs

https://radiantearth.github.io/stac-browser/#/external/raw.githubusercontent.com/uw-cryo/stac-3dep-1m/refs/heads/main/catalog/catalog.json


Some notes:

Some 1m data is stored under a workunit rather than project subfolder:
Workunit: TX_RedRiver_3Area_B2_2018 
Project: TX_Red_River_FEMA_R6_Lidar_2016_D17
1m url bu workunit https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/TX_RedRiver_3Area_B2_2018/0_file_download_links.txt


/Users/scotthenderson/GitHub/uw-cryo/stac-3dep-1m/scripts/create_static_stac.py:101: UserWarning: Project 'CA_SouthernSierra_2020_B20' onemeter_category is not 'Meets' but 'Pending publication'
