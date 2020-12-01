from __future__ import absolute_import, division, print_function, unicode_literals
import os
from src import six
import abc
from operator import attrgetter
from src import util
# must import these before xarray in order to register accessors
import cftime
import src.metpy_xr
from src.metpy_xr.units import units
import xarray as xr

@six.python_2_unicode_compatible
class DataPreprocessError(Exception):
    """Exception signaling an error in preprocessing data after it's been 
    fetched, but before any PODs run.
    """
    def __init__(self, dataset, msg=''):
        self.dataset = dataset
        self.msg = msg

    def __str__(self):
        if hasattr(self.dataset, 'name'):
            return 'Data preprocessing error for {}: {}.'.format(
                self.dataset.name, self.msg)
        else:
            return 'Data preprocessing error: {}.'.format(self.msg)

class PreprocessorFunctionBase(six.with_metaclass(abc.ABCMeta)):
    """Abstract interface for implementing a specific preprocessing functionality.
    We prefer to put each set of operations in its own child class, rather than
    dumping everything into a general Preprocessor class, in order to keep the
    logic easier to follow.
    """
    def __init__(self, data_mgr, var):
        pass

    @abc.abstractmethod
    def parse(self, xr_dataset, **kwargs):
        """Additional setup and parsing to be done based on attributes of first 
        file in dataset, before full dataset is processed.
        """
        pass

    @abc.abstractmethod
    def process_static_dataset(self, xr_dataset, **kwargs):
        """Preprocessing to be done for time-independent datasets.
        """
        return xr_dataset

    @abc.abstractmethod
    def process_file(self, xr_dataset, **kwargs):
        """Preprocessing to be done for each individual file of a time-dependent
        dataset, before :meth:`process_dataset` is called.
        """
        return xr_dataset

    @abc.abstractmethod
    def process_dataset(self, xr_dataset, **kwargs):
        """Preprocessing to be done for time-dependent datasets.
        """
        return xr_dataset

class CropDateRangeFunction(PreprocessorFunctionBase):
    """A :class:`PreprocessorFunctionBase` which trims the time axis of the
    dataset to the user-requested analysis period.
    """
    @staticmethod
    def cast_to_cftime(dt, calendar):
        """HACK to cast python datetime to cftime.datetime with given calendar.
        """
        # NB "tm_mday" is not a typo
        t = dt.timetuple()
        tt = (getattr(t, attr_) for attr_ in 
            ('tm_year', 'tm_mon', 'tm_mday', 'tm_hour', 'tm_min', 'tm_sec'))
        return cftime.datetime(*tt, calendar=calendar)

    def crop_time_axis(self, ds, ax_names, calendar, date_range, v, **kwargs):
        """Parse quantities related to the calendar for time-dependent data.
        In particular, ``date_range`` was set from user input before we knew the 
        model's calendar. HACK here to cast those values into `cftime.datetime 
        <https://unidata.github.io/cftime/api.html#cftime.datetime>`__
        objects so they can be compared with the model data's time axis.

        Args:
            ds: `xarray.Dataset 
                <http://xarray.pydata.org/en/stable/generated/xarray.Dataset.html>`__ 
                instance.
        """
        if 'T' not in ax_names:
            print('\tWarning: tried to crop time axis of time-independent variable')
            return ds
        # lower/upper are earliest/latest datetimes consistent with the datetime 
        # we were given, to that precision (eg lower for "2000" would be 
        # jan 1, 2000, and upper would be dec 31).
        dt_start_lower = self.cast_to_cftime(date_range.start.lower, calendar)
        dt_start_upper = self.cast_to_cftime(date_range.start.upper, calendar)
        dt_end_lower = self.cast_to_cftime(date_range.end.lower, calendar)
        dt_end_upper = self.cast_to_cftime(date_range.end.upper, calendar)

        time_ax = ds[ax_names['T']] # abbreviate
        if time_ax.values[0] > dt_start_upper:
            error_str = ("Error: dataset start ({}) is after requested date "
                "range start ({})").format(time_ax.values[0], dt_start_upper)
            print('\t' + error_str)
            raise DataPreprocessError(v, error_str)
        if time_ax.values[-1] < dt_end_lower:
            error_str = ("Error: dataset end ({}) is before requested date "
                "range end ({})").format(time_ax.values[-1], dt_end_lower)
            print('\t' + error_str)
            raise DataPreprocessError(v, error_str)
        
        print("\ttrimming '{}' of {} from {}-{} to {}".format(
                ax_names['T'], ax_names['var'],
                time_ax.values[0], time_ax.values[-1], date_range
            ))
        return ds.sel(**({
            ax_names['T']: slice(dt_start_lower, dt_end_upper)
        }))

    def parse(self, ds, **kwargs):
        pass

    def process_static_dataset(self, ds, **kwargs):
        return ds

    def process_file(self, ds, **kwargs):
        return ds

    def process_dataset(self, ds, **kwargs):
        return self.crop_time_axis(ds, **kwargs)

class ExtractLevelFunction(PreprocessorFunctionBase):
    """Extract a single pressure level from a DataSet. Unit conversions of 
    pressure are handled by metpy, but paramateric vertical coordinates are not
    handled (since that would require interpolation.) If the exact level is not
    provided by the data, DataPreprocessError is raised.  

    Args:
        ds: `xarray.Dataset 
            <http://xarray.pydata.org/en/stable/generated/xarray.Dataset.html>`__ 
            instance.

    TODO: Properly translate vertical coordinate name and units. If passed 3D
    data, verify that it's for the requested level. Rename variable according to
    convention POD expects.
    """
    def extract_level(self, ds, ax_names, v, **kwargs):
        if 'Z' not in ax_names \
            or 'pressure' not in getattr(v, 'scalar_coordinates', dict()):
            return ds
        p_level = int(v.scalar_coordinates['pressure'])
        try:
            ds = ds.metpy.sel(**({ax_names['Z']: p_level * units.hPa}))
            # rename dependent variable
            return ds.rename({ax_names['var']: ax_names['var']+str(p_level)})
        except KeyError:
            # level wasn't present in coordinate axis
            raise DataPreprocessError(("Pressure axis of file didn't provide "
                f"requested level {p_level}."))

    def parse(self, ds, **kwargs):
        pass

    def process_static_dataset(self, ds, **kwargs):
        return ds

    def process_file(self, ds, **kwargs):
        # Do the level extraction here, on a per-file basis, to minimize the
        # data volume kept in memory.
        return self.extract_level(ds, **kwargs)

    def process_dataset(self, ds, **kwargs):
        return ds

# ==================================================

class MDTFPreprocessorBase(six.with_metaclass(abc.ABCMeta)):
    """Base class for preprocessing data after it's been fetched, in order to 
    put it into a format expected by PODs. The only functionality implemented 
    here is parsing data axes and CF attributes; all other functionality is 
    provided by :class:`PreprocessorFunctionBase` functions.
    """
    _default_functions = []

    def __init__(self, data_mgr, var, functions=None):
        self.date_range = data_mgr.date_range
        self.data_freq = data_mgr.data_freq
        self.convention = data_mgr.convention
        self.ax_names = dict()
        self.calendar = None

        self.v = var
        assert var.remote_data
        if len(var.remote_data) > 1:
            self.files = sorted(
                # should have sorted at end of data query?
                var.remote_data, key=attrgetter('date_range.start')
            )
        else:
            self.files = var.remote_data
        # initialize PreprocessorFunctionBase objects
        self.functions = [cls_(data_mgr, var) for cls_ in self._default_functions]
        if functions:
            self.functions.extend([cls_(data_mgr, var) for cls_ in functions])

    # arguments passed to open_dataset, open_mfdataset, to_netcdf
    netcdf_kwargs = {
        "engine": "netcdf4"
    }
    # arguments passed to open_dataset and open_mfdataset
    open_dataset_kwargs = {
        "decode_coords": True, # parse coords attr
        "decode_cf": False,    # don't decode CF on open: done in parse_cf_wrapper instead
        "decode_times": False, # don't decode time axis into default np.datetime64 objects
        "use_cftime": True     # use cftime library for dates/calendars instead
    }
    open_dataset_kwargs.update(netcdf_kwargs)

    @staticmethod
    def parse_cf_wrapper(ds):
        """Wrapper to pre-process netcdf attributes before calling xarray's
        `decode_cf <http://xarray.pydata.org/en/stable/generated/xarray.decode_cf.html>`__
        method and defining metpy's `accessors 
        <https://unidata.github.io/MetPy/latest/api/generated/metpy.xarray.html>`__.

        Args:
            ds: `xarray.Dataset 
                <http://xarray.pydata.org/en/stable/generated/xarray.Dataset.html>`__ 
                instance.

        Returns xarray.Dataset instance with CF attributes parsed and defined.
        """
        def _strip(v):
            return (v.strip() if isinstance(v, str) else v)
        def _strip_dict(d):
            return {_strip(k): _strip(v) for k,v in d.items()}
    
        # Handle previously encountered case where model data attributes 
        # contained whitespace. Strip whitepsace from attrs before calling 
        # decode_cf, since malformed metadata will raise errors.
        ds.attrs = _strip_dict(ds.attrs)
        for var in ds.variables:
            ds[var].attrs = _strip_dict(ds[var].attrs)
        ds = xr.decode_cf(
            ds, decode_times=True, decode_coords=True, use_cftime=True, 
            decode_timedelta=None
        )
        return ds.metpy.parse_cf()

    def parse_axes(self, ds):
        """Use metpy accessors to determine the names used for X,Y,Z,T and other
        dimensions of the data.
        """
        def _find_var_name(ds, expected_name):
            if expected_name in ds.data_vars:
                return expected_name
            # dependent variable wasn't found by its expected name; try to find
            # it assuming it's the variable with the largest rank.
            dim_lookup = util.MultiMap({var: ds[var].ndim for var in ds.data_vars})
            d_max = util.coerce_from_iter(
                max(dim_lookup.values(), key=util.coerce_from_iter)
            )
            var_name = dim_lookup.inverse_get_(d_max)
            if not isinstance(var_name, str): 
                # returned a set of (!=1) variables with same rank
                raise ValueError("Couldn't determine var")
            else:
                print("\tWarning: Expected {} not found in file, using {}".format(
                    expected_name, var_name))
                return var_name

        def _metpy_lookup(ds, var_name, metpy_attr):
            try:
                return getattr(ds[var_name].metpy, metpy_attr).name
            except AttributeError:
                # metpy couldn't find this axis, maybe because ds doesn't have it
                return None

        var_name = _find_var_name(ds, self.v.name_in_model)
        self.ax_names['var'] = var_name

        metpy_attrs = {'X':'x', 'Y':'y', 'Z':'vertical', 'T':'time'}
        for k,v in metpy_attrs.items():
            ax_name = _metpy_lookup(ds, var_name, v)
            if ax_name:
                self.ax_names[k] = ax_name
        # in case data has other axes (eg wavelength) that metpy doesn't handle
        # punt on it for now and label them as W0, W1, ...
        other_axes = set(ds[var_name].dims).difference(list(self.ax_names.values()))
        for i, ax_name in enumerate(other_axes):
            self.ax_names['W'+str(i)] = ax_name

    def parse_calendar(self, ds):
        """Parse the calendar for time-dependent data (assumes CF conventions).
        """
        def _check_backup_location(dict_):
            if (not self.calendar) and 'calendar' in dict_:
                self.calendar = dict_['calendar'].lower().strip()

        time = self.ax_names['T']
        self.calendar = getattr(ds[time].values[0], 'calendar', None)
        if self.calendar is None:
            print('\tWarning: cftime calendar info parse failed.')
            _check_backup_location(ds[time].attrs)
            _check_backup_location(ds.attrs)
            _check_backup_location(self.convention)
        if self.calendar is None:
            raise ValueError("No calendar info in file.")

    def parse(self, xr_dataset):
        """Additional setup and parsing to be done based on attributes of first 
        file in dataset, before full dataset is processed.
        """
        kwargs = self.__dict__
        xr_dataset = self.parse_cf_wrapper(xr_dataset)
        self.parse_axes(xr_dataset)
        if 'T' in self.ax_names:
            self.parse_calendar(xr_dataset)
        for func in self.functions:
            func.parse(xr_dataset, **kwargs)

    def process_static_dataset(self, xr_dataset):
        """Preprocessing to be done for time-independent datasets.
        """
        kwargs = self.__dict__
        for func in self.functions:
            func.process_static_dataset(xr_dataset, **kwargs)
        return xr_dataset

    def process_file(self, xr_dataset):
        """Preprocessing to be done for each individual file of a time-dependent
        dataset, before :meth:`process_dataset` is called.
        """
        kwargs = self.__dict__
        xr_dataset = self.parse_cf_wrapper(xr_dataset)
        for func in self.functions:
            xr_dataset = func.process_file(xr_dataset, **kwargs)
        return xr_dataset

    def process_dataset(self, xr_dataset):
        """Preprocessing to be done for time-dependent datasets.
        """
        kwargs = self.__dict__
        for func in self.functions:
            xr_dataset = func.process_dataset(xr_dataset, **kwargs)
        return xr_dataset

    @abc.abstractmethod
    def preprocess(self):
        """Top-level wrapper for doing all preprocessing of data files. This is 
        the only user-facing method after instance has been init'ed.
        """
        pass


class SingleFilePreprocessor(MDTFPreprocessorBase):
    """A :class:`MDTFPreprocessorBase` for preprocessing model data that is 
    provided as a single netcdf file per variable, for example the sample model
    data.
    """
    def preprocess(self):
        assert len(self.files) == 1
        ds = xr.open_dataset(
            self.files[0].local_path, **self.open_dataset_kwargs
        )
        self.parse(ds)
        if self.v.date_range.is_static:
            ds = self.process_static_dataset(ds)
        else:
            ds = self.process_file(ds)
            ds = self.process_dataset(ds)
        ds.to_netcdf(
            path=self.v.dest_path,
            mode='w',
            format="NETCDF3_64BIT",
            **self.netcdf_kwargs
            # don't make time unlimited, since data might be static and we 
            # analyze a fixed date range
        )
        ds.close() # save memory; shouldn't be necessary
        del ds

class DaskMultiFilePreprocessor(MDTFPreprocessorBase):
    """A :class:`MDTFPreprocessorBase` that uses xarray's dask support to 
    preprocessing model data provided as one or several netcdf files per 
    variable.
    """
    def preprocess(self):
        ds = xr.open_dataset(
            self.files[0].local_path, **self.open_dataset_kwargs
        )
        self.parse(ds)
        if self.v.date_range.is_static:
            # skip date trimming logic for time-independent files
            assert len(self.files) == 1
            ds = self.process_static_dataset(ds)
        else:
            ds.close() # save memory; shouldn't be necessary
            ds = xr.open_mfdataset(
                [f.local_path for f in self.files],
                concat_dim=self.ax_names['T'],
                combine="by_coords",
                # all non-concat'ed vars, attrs must be the same:
                compat="identical",
                preprocess=self.process_file,
                # only time-dependent variables and coords are concat'ed:
                data_vars="minimal", coords="minimal",
                # use dask
                parallel=True,
                # raise ValueError if non-time dims conflict:
                join="exact",
                **self.open_dataset_kwargs
            )
            ds = self.process_dataset(ds)
        ds.to_netcdf(
            path=self.v.dest_path,
            mode='w',
            format="NETCDF3_64BIT",
            **self.netcdf_kwargs
            # don't make time unlimited, since data might be static and we 
            # analyze a fixed date range
        )
        ds.close() # save memory; shouldn't be necessary
        del ds

# -------------------------------------------------

class SamplemodeldataPreprocessor(SingleFilePreprocessor):
    """A :class:`MDTFPreprocessorBase` intended for use on sample model data
    only. Assumes all data is in one netCDF file and only truncates the date
    range.
    """
    _default_functions = [CropDateRangeFunction]

class MdtfdataPreprocessor(DaskMultiFilePreprocessor):
    """A :class:`MDTFPreprocessorBase` for general, multi-file data.
    """
    _default_functions = [CropDateRangeFunction, ExtractLevelFunction]