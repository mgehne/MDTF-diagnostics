import os
import shutil
import datelabel
from util import run_command, check_executable

class NetcdfHelper(object):
    def __init__(self):
        raise NotImplementedError(
            """NetcdfHelper is a stub defining method signatures for netcdf 
            manipulation wrapper functions, and shouldn't be called directly."""
            )

    @staticmethod
    def nc_check_environ():
        pass

    @staticmethod
    def nc_cat_chunks(chunk_list, out_file, working_dir=None):
        raise NotImplementedError(
            """NetcdfHelper is a stub defining method signatures for netcdf 
            manipulation wrapper functions, and shouldn't be called directly."""
            )

    @staticmethod
    def nc_crop_time_axis(time_var_name, date_range, in_file, 
        out_file=None, working_dir=None):   
        raise NotImplementedError(
            """NetcdfHelper is a stub defining method signatures for netcdf 
            manipulation wrapper functions, and shouldn't be called directly."""
            )

    @staticmethod
    def nc_extract_level():
        raise NotImplementedError(
            """NetcdfHelper is a stub defining method signatures for netcdf 
            manipulation wrapper functions, and shouldn't be called directly."""
            )


class NcoNetcdfHelper(NetcdfHelper):
    # Just calls command-line utilities, doesn't use PyNCO bindings
    @staticmethod
    def nc_check_environ():
        # check nco exists
        if not check_executable('ncks'):
            raise OSError('NCO utilities not found on $PATH.')

    @staticmethod
    def nc_cat_chunks(chunk_list, out_file, working_dir=None):
        if working_dir is None:
            working_dir = os.getcwd()
        # not running in shell, so can't use glob expansion.
        run_command(['ncrcat', '--no_tmp_fl', '-O'] + chunk_list + [out_file], 
            cwd=working_dir)

    @staticmethod
    def nc_crop_time_axis(time_var_name, date_range, in_file, 
        out_file=None, working_dir=None):
        if out_file is None:
            out_file = 'MDTF_temp.nc'
            move_back = True
        else:
            move_back = False
        if working_dir is None:
            working_dir = os.getcwd()
        ncks_time_format = '%Y-%m-%d %H:%M:%S'
        # don't need to quote time strings in args to ncks because it's not 
        # being called by a shell
        run_command(
            ['ncks', '--no_tmp_fl', '-O', '-d', "{},{},{}".format(
                time_var_name, 
                date_range.start.strftime(ncks_time_format),
                date_range.end.strftime(ncks_time_format)
            ), in_file, out_file],
            cwd=working_dir)
        if move_back:
            # manually move file back 
            cwd = os.getcwd()
            os.chdir(working_dir)
            os.remove(in_file)
            shutil.move(out_file, in_file)
            os.chdir(cwd)


class CdoNetcdfHelper(NetcdfHelper):
    pass