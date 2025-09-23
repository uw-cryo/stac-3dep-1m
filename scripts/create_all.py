#!/usr/bin/env python

# NOt sure if there is an easier way to do this
import boto3
from botocore import UNSIGNED
from botocore.client import Config
import subprocess
from pathlib import Path

s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
response = s3.list_objects_v2(Bucket='prd-tnm', Prefix='StagedProducts/Elevation/1m/Projects/', Delimiter='/')
folders = [content['Prefix'] for content in response.get('CommonPrefixes')]
all_folders = [x.split('/')[-2] for x in folders]

print('Located {} folders in S3...'.format(len(all_folders)))

# For some reason these are mising TIFFs
NO_TIFFS = [
    "CA_LakeCounty_2015",
    "CA_SanDiegoQL2_2014",
    "ME_Western_2016",
    "MI_25County_AlleganCo_2015",
    "MI_31Co_Ottawa_2016",
    "MI_13Co_BerrienCO_2015",
    "MI_13Co_Emmett_2015",
    "MI_13Co_VanBurrenCo_2015",
    "MI_AlgerCo_2015",
    "MI_Baraga_2015",
    "MI_BenzieCo_2015",
    "MI_BranchCo_2017",
    "MI_DeltaCo_2015",
    "MI_CalhounCo_2017",
    "MI_GrandTraverseCO_2015",
    "MI_HiawathaNF_QL1_2018",
    "MI_HiawathaNF_QL2_2018",
    "MI_LeelanauCo_2015",
    "MI_MackinacCo_2015",
    "MI_ManisteeCo_2015",
    "MI_Marquette_2015",
    "NY_ClintonEssexLakeChamplain_2014",
]

# For efficiency only work with folders that don't already have a STAC
catalog_path = Path('catalog')
subfolders = [f.name for f in catalog_path.iterdir() if f.is_dir()]
missing_folders = set(all_folders) - set(subfolders) - set(NO_TIFFS)

print('Processing {} folders without local STAC...'.format(len(missing_folders)))
print('\n'.join(sorted(missing_folders)))

# Non-elegant approach to creating all STACs for workunit or project
for i, project in enumerate(missing_folders):
    print(i, project)
    try:
        subprocess.run(['./scripts/create_static_stac.py', '--workunit', project], check=True, capture_output=False)
    except subprocess.CalledProcessError:
        print('Failed processing as WESM Workunit, trying as Project...')
        try:
            subprocess.run(['./scripts/create_static_stac.py', '--project', project], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            print('Failed processing as Project... skipping.')
