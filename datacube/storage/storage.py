# coding=utf-8
"""
Create/store dataset data into storage units based on the provided storage mappings
"""
from __future__ import absolute_import, division, print_function

import logging
from contextlib import contextmanager
from pathlib import Path

from datacube.model import CRS
from datacube.storage import netcdf_writer
from datacube.options import OPTIONS

try:
    from yaml import CSafeDumper as SafeDumper
except ImportError:
    from yaml import SafeDumper
import numpy

import rasterio.warp
import rasterio.crs
from rasterio.warp import RESAMPLING

from datacube.utils import clamp, datetime_to_seconds_since_1970

_LOG = logging.getLogger(__name__)

RESAMPLING_METHODS = {
    'nearest': RESAMPLING.nearest,
    'cubic': RESAMPLING.cubic,
    'bilinear': RESAMPLING.bilinear,
    'cubic_spline': RESAMPLING.cubic_spline,
    'lanczos': RESAMPLING.lanczos,
    'average': RESAMPLING.average,
}

assert str(rasterio.__version__) >= '0.34.0', "rasterio version 0.34.0 or higher is required"
GDAL_NETCDF_TIME = ('NETCDF_DIM_'
                    if str(rasterio.__gdal_version__) >= '1.10.0' else
                    'NETCDF_DIMENSION_') + 'time'


def _rasterio_resampling_method(resampling):
    return RESAMPLING_METHODS[resampling.lower()]


if str(rasterio.__version__) >= '0.36.0':
    def _rasterio_crs_wkt(src):
        return str(src.crs.wkt)
else:
    def _rasterio_crs_wkt(src):
        return str(src.crs_wkt)


def _calc_offsets(off, src_size, dst_size):
    """
    >>> _calc_offsets(11, 10, 12) # no overlap
    (10, 0, 0)
    >>> _calc_offsets(-11, 12, 10) # no overlap
    (0, 10, 0)
    >>> _calc_offsets(5, 10, 12) # overlap
    (5, 0, 5)
    >>> _calc_offsets(-5, 12, 10) # overlap
    (0, 5, 5)
    >>> _calc_offsets(5, 10, 4) # containment
    (5, 0, 4)
    >>> _calc_offsets(-5, 4, 10) # containment
    (0, 5, 4)
    """
    read_off = clamp(off, 0, src_size)
    write_off = clamp(-off, 0, dst_size)
    size = min(src_size-read_off, dst_size-write_off)
    return read_off, write_off, size


def fuse_sources(sources, destination, dst_transform, dst_projection, dst_nodata, resampling='nearest', fuse_func=None):
    assert len(destination.shape) == 2

    resampling = _rasterio_resampling_method(resampling)

    def no_scale(affine, eps=0.01):
        return abs(affine.a - 1.0) < eps and abs(affine.e - 1.0) < eps

    def no_fractional_translate(affine, eps=0.01):
        return abs(affine.c % 1.0) < eps and abs(affine.f % 1.0) < eps

    def reproject(source, dest):
        with source.open() as src:
            array_transform = ~source.transform * dst_transform
            if (source.crs == dst_projection and no_scale(array_transform) and
                    (resampling == RESAMPLING.nearest or no_fractional_translate(array_transform))):
                dydx = (int(round(array_transform.f)), int(round(array_transform.c)))
                read, write, shape = zip(*map(_calc_offsets, dydx, src.shape, dest.shape))

                dest.fill(dst_nodata)
                if all(shape):
                    window = ((read[0], read[0] + shape[0]), (read[1], read[1] + shape[1]))
                    tmp = src.ds.read(indexes=src.bidx, window=window)
                    numpy.copyto(dest[write[0]:write[0] + shape[0], write[1]:write[1] + shape[1]],
                                 tmp, where=(tmp != source.nodata))
            else:
                rasterio.warp.reproject(src,
                                        dest,
                                        src_transform=source.transform,
                                        src_crs=str(source.crs),
                                        src_nodata=source.nodata,
                                        dst_transform=dst_transform,
                                        dst_crs=str(dst_projection),
                                        dst_nodata=dst_nodata,
                                        resampling=resampling,
                                        NUM_THREADS=OPTIONS['reproject_threads'])

    def copyto_fuser(dest, src):
        numpy.copyto(dest, src, where=(src != dst_nodata))

    fuse_func = fuse_func or copyto_fuser

    if len(sources) == 1:
        reproject(sources[0], destination)
        return destination

    destination.fill(dst_nodata)
    if len(sources) == 0:
        return destination

    buffer_ = numpy.empty(destination.shape, dtype=destination.dtype)
    for source in sources:
        reproject(source, buffer_)
        fuse_func(destination, buffer_)

    return destination


class DatasetSource(object):
    def __init__(self, dataset, measurement_id):
        """

        :type dataset: datacube.model.Dataset
        :param measurement_id:
        """
        self._bandinfo = dataset.type.measurements[measurement_id]
        self._descriptor = dataset.measurements[measurement_id]
        self.transform = None
        self.crs = dataset.crs
        self.dtype = None
        self.nodata = None
        self.format = dataset.format
        self.time = dataset.center_time
        self.local_path = dataset.local_path

    @contextmanager
    def open(self):
        if self._descriptor['path']:
            if Path(self._descriptor['path']).is_absolute():
                filename = self._descriptor['path']
            else:
                filename = str(self.local_path.parent.joinpath(self._descriptor['path']))
        else:
            filename = str(self.local_path)

        for nasty_format in ('netcdf', 'hdf'):
            if nasty_format in self.format.lower():
                filename = 'file://%s:%s:%s' % (self.format, filename, self._descriptor['layer'])
                bandnumber = None
                break
        else:
            bandnumber = self._descriptor.get('layer', 1)

        try:
            _LOG.debug("openening %s, band %s", filename, bandnumber)
            with rasterio.open(filename) as src:

                if bandnumber is None:
                    if 'netcdf' in self.format.lower():
                        bandnumber = self.wheres_my_band(src, self.time)
                    else:
                        bandnumber = 1

                self.transform = src.affine

                try:
                    self.crs = CRS(_rasterio_crs_wkt(src))
                except ValueError:
                    pass
                self.dtype = numpy.dtype(src.dtypes[0])
                self.nodata = self.dtype.type(src.nodatavals[0] if src.nodatavals[0] is not None else
                                              self._bandinfo.get('nodata'))
                yield rasterio.band(src, bandnumber)
        except Exception as e:
            _LOG.error("Error opening source dataset: %s", filename)
            raise e

    def wheres_my_band(self, src, time):
        sec_since_1970 = datetime_to_seconds_since_1970(time)

        idx = 0
        dist = float('+inf')
        for i in range(1, src.count+1):
            v = float(src.tags(i)[GDAL_NETCDF_TIME])
            if abs(sec_since_1970 - v) < dist:
                idx = i
                dist = abs(sec_since_1970 - v)
        return idx


def create_netcdf_storage_unit(filename,
                               crs, coordinates, variables, variable_params, global_attributes=None,
                               netcdfparams=None):
    if filename.exists():
        raise RuntimeError('Storage Unit already exists: %s' % filename)

    try:
        filename.parent.mkdir(parents=True)
    except OSError:
        pass

    nco = netcdf_writer.create_netcdf(str(filename), **(netcdfparams or {}))

    for name, coord in coordinates.items():
        netcdf_writer.create_coordinate(nco, name, coord.values, coord.units)

    netcdf_writer.create_grid_mapping_variable(nco, crs)

    for name, variable in variables.items():
        set_crs = all(dim in variable.dims for dim in crs.dimensions)
        var_params = variable_params.get(name, {})
        data_var = netcdf_writer.create_variable(nco, name, variable, set_crs=set_crs, **var_params)

        for key, value in var_params.get('attrs', {}).items():
            setattr(data_var, key, value)

    for key, value in (global_attributes or {}).items():
        setattr(nco, key, value)

    return nco


def write_dataset_to_netcdf(access_unit, global_attributes, variable_params, filename, netcdfparams=None):
    nco = create_netcdf_storage_unit(filename,
                                     access_unit.crs,
                                     access_unit.coords,
                                     access_unit.data_vars,
                                     variable_params,
                                     global_attributes,
                                     netcdfparams)

    for name, variable in access_unit.data_vars.items():
        nco[name][:] = netcdf_writer.netcdfy_data(variable.values)

    nco.close()
