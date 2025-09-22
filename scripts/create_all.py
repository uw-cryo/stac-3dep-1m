#!/usr/bin/env python

# NOt sure if there is an easier way to do this
import boto3
from botocore import UNSIGNED
from botocore.client import Config
import subprocess

s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
response = s3.list_objects_v2(Bucket='prd-tnm', Prefix='StagedProducts/Elevation/1m/Projects/', Delimiter='/')
folders = [content['Prefix'] for content in response.get('CommonPrefixes')]
all_folders = [x.split('/')[-2] for x in folders]
print('Creating STAC for {} projects...'.format(len(all_folders)))

# Non-elegant approach to creating all STACs for workunit or project
for i, project in enumerate(all_folders):
    print(i, project)
    try:
        subprocess.run(['./scripts/create_static_stac.py', '--workunit', project], check=True, capture_output=False)
    except subprocess.CalledProcessError:
        print('Failed processing as WESM Workunit, trying as Project...')
        try:
            subprocess.run(['./scripts/create_static_stac.py', '--project', project], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            print('Failed processing as Project... skipping.')
