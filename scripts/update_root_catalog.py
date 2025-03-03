#!/usr/bin/env python
"""
Keep top-level catalog up-to date with subcatalogs
"""
import pathlib
import pystac

def get_catalogs(base_path):
    base_path = pathlib.Path(base_path)
    return [pystac.read_file(path) for path in base_path.rglob("./*/catalog.json")]

def update_root_catalog():
    path = 'catalog/catalog.json'
    cat = pystac.read_file(path)
    subcats = get_catalogs("catalog")
    existing = [subcat.id for subcat in list(cat.get_children())]
    new = [x for x in subcats if x.id not in existing]
    cat.add_children(new)
    cat.save_object(include_self_link=False, dest_href='catalog/test.json')

if __name__ == "__main__":
    update_root_catalog()
