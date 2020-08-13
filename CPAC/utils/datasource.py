import json
import nipype.pipeline.engine as pe
import nipype.interfaces.utility as util

from CPAC.utils import function


def get_rest(scan, rest_dict, resource="scan"):
    """Return the file path of the chosen resource stored in the functional
    file dictionary, if it exists.

    scan: the scan/series name or label
    rest_dict: the dictionary read in from the data configuration YAML file
               (sublist) nested under 'func:'
    resource: the dictionary key
                  scan - the functional timeseries
                  scan_parameters - path to the scan parameters JSON file, or
                                    a dictionary containing scan parameters
                                    information (to be phased out in the
                                    future)
    """
    try:
        file_path = rest_dict[scan][resource]
    except KeyError:
        file_path = None
    return file_path


def extract_scan_params_dct(scan_params_dct):
    return scan_params_dct


def get_map(map, map_dct):
    # return the spatial map required
    return map_dct[map]


def select_model_files(model, ftest, model_name):
    """
    Method to select model files
    """

    import os
    import glob

    files = glob.glob(os.path.join(model, '*'))

    if len(files) == 0:
        raise Exception("No files found inside directory %s" % model)

    fts_file = ''

    for filename in files:
        if (model_name + '.mat') in filename:
            mat_file = filename
        elif (model_name + '.grp') in filename:
            grp_file = filename
        elif ((model_name + '.fts') in filename) and ftest:
            fts_file = filename
        elif (model_name + '.con') in filename:
            con_file = filename

    if ftest == True and fts_file == '':
        errmsg = "\n[!] CPAC says: You have f-tests included in your group " \
                 "analysis model '%s', but no .fts files were found in the " \
                 "output folder specified for group analysis: %s.\n\nThe " \
                 ".fts file is automatically generated by CPAC, and if you " \
                 "are seeing this error, it is because something went wrong " \
                 "with the generation of this file, or it has been moved." \
                 "\n\n" % (model_name, model)

        raise Exception(errmsg)

    return fts_file, con_file, grp_file, mat_file


def check_func_scan(func_scan_dct, scan):
    """Run some checks on the functional timeseries-related files for a given
    series/scan name or label."""

    scan_resources = func_scan_dct[scan]

    try:
        scan_resources.keys()
    except AttributeError:
        err = "\n[!] The data configuration file you provided is " \
              "missing a level under the 'func:' key. CPAC versions " \
              "1.2 and later use data configurations with an " \
              "additional level of nesting.\n\nExample\nfunc:\n  " \
              "rest01:\n    scan: /path/to/rest01_func.nii.gz\n" \
              "    scan parameters: /path/to/scan_params.json\n\n" \
              "See the User Guide for more information.\n\n"
        raise Exception(err)

    # actual 4D time series file
    if "scan" not in scan_resources.keys():
        err = "\n\n[!] The {0} scan is missing its actual time-series " \
              "scan file, which should be a filepath labeled with the " \
              "'scan' key.\n\n".format(scan)
        raise Exception(err)

    # Nipype restriction (may have changed)
    if '.' in scan or '+' in scan or '*' in scan:
        raise Exception('\n\n[!] Scan names cannot contain any special '
                        'characters (., +, *, etc.). Please update this '
                        'and try again.\n\nScan: {0}'
                        '\n\n'.format(scan))


def create_func_datasource(rest_dict, wf_name='func_datasource'):
    """Return the functional timeseries-related file paths for each
    series/scan, from the dictionary of functional files described in the data
    configuration (sublist) YAML file.

    Scan input (from inputnode) is an iterable.
    """
    import nipype.pipeline.engine as pe
    import nipype.interfaces.utility as util

    wf = pe.Workflow(name=wf_name)

    inputnode = pe.Node(util.IdentityInterface(
                                fields=['subject', 'scan', 'creds_path',
                                        'dl_dir'],
                                mandatory_inputs=True),
                        name='inputnode')

    outputnode = pe.Node(util.IdentityInterface(fields=['subject', 'rest',
                                                        'scan', 'scan_params',
                                                        'phase_diff',
                                                        'magnitude']),
                         name='outputspec')

    # have this here for now because of the big change in the data
    # configuration format
    check_scan = pe.Node(function.Function(input_names=['func_scan_dct',
                                                        'scan'],
                                           output_names=[],
                                           function=check_func_scan,
                                           as_module=True),
                         name='check_func_scan')

    check_scan.inputs.func_scan_dct = rest_dict
    wf.connect(inputnode, 'scan', check_scan, 'scan')

    # get the functional scan itself
    selectrest = pe.Node(function.Function(input_names=['scan',
                                                        'rest_dict',
                                                        'resource'],
                                           output_names=['file_path'],
                                           function=get_rest,
                                           as_module=True),
                         name='selectrest')
    selectrest.inputs.rest_dict = rest_dict
    selectrest.inputs.resource = "scan"
    wf.connect(inputnode, 'scan', selectrest, 'scan')

    # check to see if it's on an Amazon AWS S3 bucket, and download it, if it
    # is - otherwise, just return the local file path
    check_s3_node = pe.Node(function.Function(input_names=['file_path',
                                                           'creds_path',
                                                           'dl_dir',
                                                           'img_type'],
                                              output_names=['local_path'],
                                              function=check_for_s3,
                                              as_module=True),
                            name='check_for_s3')

    wf.connect(selectrest, 'file_path', check_s3_node, 'file_path')
    wf.connect(inputnode, 'creds_path', check_s3_node, 'creds_path')
    wf.connect(inputnode, 'dl_dir', check_s3_node, 'dl_dir')
    check_s3_node.inputs.img_type = 'func'

    wf.connect(inputnode, 'subject', outputnode, 'subject')
    wf.connect(check_s3_node, 'local_path', outputnode, 'rest')
    wf.connect(inputnode, 'scan', outputnode, 'scan')

    # scan parameters CSV
    select_scan_params = pe.Node(function.Function(input_names=['scan',
                                                                'rest_dict',
                                                                'resource'],
                                                   output_names=['file_path'],
                                                   function=get_rest,
                                                   as_module=True),
                                 name='select_scan_params')
    select_scan_params.inputs.rest_dict = rest_dict
    select_scan_params.inputs.resource = "scan_parameters"
    wf.connect(inputnode, 'scan', select_scan_params, 'scan')

    # if the scan parameters file is on AWS S3, download it
    s3_scan_params = pe.Node(function.Function(input_names=['file_path',
                                                            'creds_path',
                                                            'dl_dir',
                                                            'img_type'],
                                               output_names=['local_path'],
                                               function=check_for_s3,
                                               as_module=True),
                             name='s3_scan_params')

    wf.connect(select_scan_params, 'file_path', s3_scan_params, 'file_path')
    wf.connect(inputnode, 'creds_path', s3_scan_params, 'creds_path')
    wf.connect(inputnode, 'dl_dir', s3_scan_params, 'dl_dir')
    wf.connect(s3_scan_params, 'local_path', outputnode, 'scan_params')

    return wf


def create_fmap_datasource(fmap_dct, wf_name='fmap_datasource'):
    """Return the field map files, from the dictionary of functional files
    described in the data configuration (sublist) YAML file.
    """

    import nipype.pipeline.engine as pe
    import nipype.interfaces.utility as util

    wf = pe.Workflow(name=wf_name)

    inputnode = pe.Node(util.IdentityInterface(
                                fields=['subject', 'scan', 'creds_path',
                                        'dl_dir'],
                                mandatory_inputs=True),
                        name='inputnode')

    outputnode = pe.Node(util.IdentityInterface(fields=['subject', 'rest',
                                                        'scan', 'scan_params',
                                                        'phase_diff',
                                                        'magnitude']),
                         name='outputspec')

    selectrest = pe.Node(function.Function(input_names=['scan',
                                                        'rest_dict',
                                                        'resource'],
                                           output_names=['file_path'],
                                           function=get_rest,
                                           as_module=True),
                         name='selectrest')
    selectrest.inputs.rest_dict = fmap_dct
    selectrest.inputs.resource = "scan"
    wf.connect(inputnode, 'scan', selectrest, 'scan')

    # check to see if it's on an Amazon AWS S3 bucket, and download it, if it
    # is - otherwise, just return the local file path
    check_s3_node = pe.Node(function.Function(input_names=['file_path',
                                                           'creds_path',
                                                           'dl_dir',
                                                           'img_type'],
                                              output_names=['local_path'],
                                              function=check_for_s3,
                                              as_module=True),
                            name='check_for_s3')

    wf.connect(selectrest, 'file_path', check_s3_node, 'file_path')
    wf.connect(inputnode, 'creds_path', check_s3_node, 'creds_path')
    wf.connect(inputnode, 'dl_dir', check_s3_node, 'dl_dir')
    check_s3_node.inputs.img_type = 'other'

    wf.connect(inputnode, 'subject', outputnode, 'subject')
    wf.connect(check_s3_node, 'local_path', outputnode, 'rest')
    wf.connect(inputnode, 'scan', outputnode, 'scan')

    # scan parameters CSV
    select_scan_params = pe.Node(function.Function(input_names=['scan',
                                                                'rest_dict',
                                                                'resource'],
                                                   output_names=['file_path'],
                                                   function=get_rest,
                                                   as_module=True),
                                 name='select_scan_params')
    select_scan_params.inputs.rest_dict = fmap_dct
    select_scan_params.inputs.resource = "scan_parameters"
    wf.connect(inputnode, 'scan', select_scan_params, 'scan')

    # if the scan parameters file is on AWS S3, download it
    s3_scan_params = pe.Node(function.Function(input_names=['file_path',
                                                            'creds_path',
                                                            'dl_dir',
                                                            'img_type'],
                                               output_names=['local_path'],
                                               function=check_for_s3,
                                               as_module=True),
                             name='s3_scan_params')

    wf.connect(select_scan_params, 'file_path', s3_scan_params, 'file_path')
    wf.connect(inputnode, 'creds_path', s3_scan_params, 'creds_path')
    wf.connect(inputnode, 'dl_dir', s3_scan_params, 'dl_dir')
    wf.connect(s3_scan_params, 'local_path', outputnode, 'scan_params')

    return wf


def get_fmap_phasediff_metadata(data_config_scan_params):

    if not isinstance(data_config_scan_params, dict) and \
            ".json" in data_config_scan_params:
        with open(data_config_scan_params, 'r') as f:
            data_config_scan_params = json.load(f)

    echo_time = data_config_scan_params.get("EchoTime")
    dwell_time = data_config_scan_params.get("DwellTime")
    pe_direction = data_config_scan_params.get("PhaseEncodingDirection")

    return (echo_time, dwell_time, pe_direction)


def calc_deltaTE_and_asym_ratio(dwell_time, echo_time_one, echo_time_two,
                                echo_time_three=None):

    echo_times = [echo_time_one, echo_time_two]
    if echo_time_three:
        # get only the two different ones
        echo_times = list(dict.fromkeys([echo_time_one, echo_time_two,
                                         echo_time_three]))

    # convert into milliseconds if necessary
    # these values will/should never be more than 10ms
    if ((echo_times[0] * 1000) < 10) and ((echo_times[1] * 1000) < 10):
        echo_times[0] = echo_times[0] * 1000
        echo_times[1] = echo_times[1] * 1000

    deltaTE = abs(echo_times[0] - echo_times[1])
    dwell_asym_ratio = (dwell_time / deltaTE)

    return (deltaTE, dwell_asym_ratio)


def match_epi_fmaps(bold_pedir, epi_fmap_one, epi_fmap_params_one,
                    epi_fmap_two=None, epi_fmap_params_two=None):
    """Parse the field map files in the data configuration and determine which
    ones have the same and opposite phase-encoding directions as the BOLD scan
    in the current pipeline.

    Example - parse the files under the 'fmap' level, i.e. 'epi_AP':
        anat: /path/to/T1w.nii.gz
        fmap:
          epi_AP:
            scan: /path/to/field-map.nii.gz
            scan_parameters: <config dictionary containing phase-encoding
                              direction>
        func:
          rest_1:
            scan: /path/to/bold.nii.gz
            scan_parameters: <config dictionary of BOLD scan parameters>

    1. Check PhaseEncodingDirection field in the metadata for the BOLD.
    2. Check whether there are one or two EPI's in the field map data.
    3. Grab the one or two EPI field maps.
    """

    fmap_dct = {epi_fmap_one: epi_fmap_params_one}
    if epi_fmap_two and epi_fmap_params_two:
        fmap_dct[epi_fmap_two] = epi_fmap_params_two

    opposite_pe_epi = None
    same_pe_epi = None

    for epi_scan in fmap_dct.keys():
        scan_params = fmap_dct[epi_scan]
        if not isinstance(scan_params, dict) and ".json" in scan_params:
            with open(scan_params, 'r') as f:
                scan_params = json.load(f)
        if "PhaseEncodingDirection" in scan_params:
            epi_pedir = scan_params["PhaseEncodingDirection"]
            if epi_pedir == bold_pedir:
                same_pe_epi = epi_scan
            elif epi_pedir[0] == bold_pedir[0]:
                opposite_pe_epi = epi_scan

    return (opposite_pe_epi, same_pe_epi)


def create_check_for_s3_node(name, file_path, img_type='other', creds_path=None, dl_dir=None, map_node=False):

    if map_node:
        check_s3_node = pe.MapNode(function.Function(input_names=['file_path',
                                                                  'creds_path',
                                                                  'dl_dir',
                                                                  'img_type'],
                                                     output_names=['local_path'],
                                                     function=check_for_s3,
                                                     as_module=True),
                                                     iterfield=['file_path'],
                                   name='check_for_s3_%s' % name)
    else: 
        check_s3_node = pe.Node(function.Function(input_names=['file_path',
                                                               'creds_path',
                                                               'dl_dir',
                                                               'img_type'],
                                                  output_names=['local_path'],
                                                  function=check_for_s3,
                                                  as_module=True),
                                name='check_for_s3_%s' % name)

    check_s3_node.inputs.set(
        file_path=file_path,
        creds_path=creds_path,
        dl_dir=dl_dir,
        img_type=img_type
    )

    return check_s3_node


# Check if passed-in file is on S3
def check_for_s3(file_path, creds_path=None, dl_dir=None, img_type='other',
                 verbose=False):

    # Import packages
    import os
    import nibabel as nib
    import botocore.exceptions
    from indi_aws import fetch_creds

    # Init variables
    s3_str = 's3://'
    if creds_path:
        if "None" in creds_path or "none" in creds_path or \
                "null" in creds_path:
            creds_path = None

    if dl_dir is None:
        dl_dir = os.getcwd()

    if file_path is None:
        # in case it's something like scan parameters or field map files, but
        # we don't have any
        return None

    # TODO: remove this once scan parameter input as dictionary is phased out
    if isinstance(file_path, dict):
        # if this is a dictionary, just skip altogether
        local_path = file_path
        return local_path

    if file_path.lower().startswith(s3_str):
        
        file_path = s3_str + file_path[len(s3_str):]

        # Get bucket name and bucket object
        bucket_name = file_path[len(s3_str):].split('/')[0]
        # Extract relative key path from bucket and local path
        s3_prefix = s3_str + bucket_name
        s3_key = file_path[len(s3_prefix) + 1:]
        local_path = os.path.join(dl_dir, bucket_name, s3_key)

        # Get local directory and create folders if they dont exist
        local_dir = os.path.dirname(local_path)
        if not os.path.exists(local_dir):
            try:
                os.makedirs(local_dir)
            except OSError as e:
                if e.errno != os.errno.EEXIST:
                    raise e

        if os.path.exists(local_path):
            print("{0} already exists- skipping download.".format(local_path))
        else:
            # Download file
            try:
                bucket = fetch_creds.return_bucket(creds_path, bucket_name)
                print("Attempting to download from AWS S3: {0}".format(file_path))
                bucket.download_file(Key=s3_key, Filename=local_path)
            except botocore.exceptions.ClientError as exc:
                error_code = int(exc.response['Error']['Code'])

                err_msg = str(exc)
                if error_code == 403:
                    err_msg = 'Access to bucket: "%s" is denied; using credentials '\
                              'in subject list: "%s"; cannot access the file "%s"'\
                              % (bucket_name, creds_path, file_path)
                elif error_code == 404:
                    err_msg = 'File: {0} does not exist; check spelling and try '\
                              'again'.format(os.path.join(bucket_name, s3_key))
                else:
                    err_msg = 'Unable to connect to bucket: "%s". Error message:\n%s'\
                              % (bucket_name, exc)

                raise Exception(err_msg)

            except Exception as exc:
                err_msg = 'Unable to connect to bucket: "%s". Error message:\n%s'\
                          % (bucket_name, exc)
                raise Exception(err_msg)

    # Otherwise just return what was passed in
    else:
        local_path = file_path

    # Check if it exists or it is successfully downloaded
    while not os.path.exists(local_path):
        # Resolve if symlink
        if os.path.islink(local_path):
            local_path = os.path.abspath(os.readlink(local_path))
        else:
            raise FileNotFoundError(f'File {local_path} does not exist!')

    if verbose:
        print("Downloaded file:\n{0}\n".format(local_path))

    # Check image dimensionality
    if local_path.endswith('.nii') or local_path.endswith('.nii.gz'):
        img_nii = nib.load(local_path)

        if img_type == 'anat':
            if len(img_nii.shape) != 3:
                raise IOError('File: %s must be an anatomical image with 3 '\
                              'dimensions but %d dimensions found!'
                              % (local_path, len(img_nii.shape)))
        elif img_type == 'func':
            if len(img_nii.shape) != 4:
                raise IOError('File: %s must be a functional image with 4 '\
                              'dimensions but %d dimensions found!'
                              % (local_path, len(img_nii.shape)))

    return local_path


def resolve_resolution(resolution, template, template_name, tag = None):

    import nipype.interfaces.afni as afni
    import nipype.pipeline.engine as pe
    from CPAC.utils.datasource import check_for_s3

    tagname = None
    local_path = None
    
    if "{" in template and tag is not None:
            tagname = "${" + tag + "}"
    try:
        if tagname is not None:
            local_path = check_for_s3(template.replace(tagname, str(resolution)))     
    except (IOError, OSError):
        local_path = None

    ## TODO debug - it works in ipython but doesn't work in nipype wf
    # try:
    #     local_path = check_for_s3('/usr/local/fsl/data/standard/MNI152_T1_3.438mmx3.438mmx3.4mm_brain_mask_dil.nii.gz')     
    # except (IOError, OSError):
    #     local_path = None

    if local_path is None:
        if tagname is not None:
            ref_template = template.replace(tagname, '1mm') 
            local_path = check_for_s3(ref_template)
        elif tagname is None and "s3" in template:
            local_path = check_for_s3(template)
        else:
            local_path = template    

        if "x" in str(resolution):
            resolution = tuple(float(i.replace('mm', '')) for i in resolution.split("x"))
        else:
            resolution = (float(resolution.replace('mm', '')), ) * 3

        resample = pe.Node(interface = afni.Resample(), name=template_name)
        resample.inputs.voxel_size = resolution
        resample.inputs.outputtype = 'NIFTI_GZ'
        resample.inputs.resample_mode = 'Cu'
        resample.inputs.in_file = local_path
        resample.base_dir = '.'

        resampled_template = resample.run()
        local_path = resampled_template.outputs.out_file

    return local_path


def create_anat_datasource(wf_name='anat_datasource'):

    import nipype.pipeline.engine as pe
    import nipype.interfaces.utility as util

    wf = pe.Workflow(name=wf_name)

    inputnode = pe.Node(util.IdentityInterface(
                                fields=['subject', 'anat', 'creds_path',
                                        'dl_dir', 'img_type'],
                                mandatory_inputs=True),
                        name='inputnode')

    check_s3_node = pe.Node(function.Function(input_names=['file_path',
                                                           'creds_path',
                                                           'dl_dir',
                                                           'img_type'],
                                              output_names=['local_path'],
                                              function=check_for_s3,
                                              as_module=True),
                            name='check_for_s3')

    wf.connect(inputnode, 'anat', check_s3_node, 'file_path')
    wf.connect(inputnode, 'creds_path', check_s3_node, 'creds_path')
    wf.connect(inputnode, 'dl_dir', check_s3_node, 'dl_dir')
    wf.connect(inputnode, 'img_type', check_s3_node, 'img_type')

    outputnode = pe.Node(util.IdentityInterface(fields=['subject',
                                                        'anat']),
                         name='outputspec')

    wf.connect(inputnode, 'subject', outputnode, 'subject')
    wf.connect(check_s3_node, 'local_path', outputnode, 'anat')

    # Return the workflow
    return wf


def create_roi_mask_dataflow(masks, wf_name='datasource_roi_mask'):

    import os

    mask_dict = {}

    for mask_file in masks:

        mask_file = mask_file.rstrip('\r\n')

        if mask_file.strip() == '' or mask_file.startswith('#'):
            continue

        base_file = os.path.basename(mask_file)

        try:
            valid_extensions = ['.nii', '.nii.gz']

            base_name = [
                base_file[:-len(ext)]
                for ext in valid_extensions
                if base_file.endswith(ext)
            ][0]

            if base_name in mask_dict:
                raise ValueError(
                    'Files with same name not allowed: %s %s' % (
                        mask_file,
                        mask_dict[base_name]
                    )
                )

            mask_dict[base_name] = mask_file

        except IndexError as e:
            raise Exception('Error in spatial_map_dataflow: '
                            'File extension not in .nii and .nii.gz')

        except Exception as e:
            raise e


    wf = pe.Workflow(name=wf_name)  

    inputnode = pe.Node(util.IdentityInterface(fields=['mask',
                                                       'mask_file',
                                                       'creds_path',
                                                       'dl_dir'],
                                               mandatory_inputs=True),
                        name='inputspec')

    mask_keys, mask_values = \
        zip(*mask_dict.items())

    inputnode.synchronize = True
    inputnode.iterables = [
        ('mask', mask_keys),
        ('mask_file', mask_values),
    ]

    check_s3_node = pe.Node(function.Function(input_names=['file_path',
                                                           'creds_path',
                                                           'dl_dir',
                                                           'img_type'],
                                              output_names=['local_path'],
                                              function=check_for_s3,
                                              as_module=True),
                            name='check_for_s3')

    wf.connect(inputnode, 'mask_file', check_s3_node, 'file_path')
    wf.connect(inputnode, 'creds_path', check_s3_node, 'creds_path')
    wf.connect(inputnode, 'dl_dir', check_s3_node, 'dl_dir')
    check_s3_node.inputs.img_type = 'mask'

    outputnode = pe.Node(util.IdentityInterface(fields=['out_file']),
                         name='outputspec')

    wf.connect(check_s3_node, 'local_path', outputnode, 'out_file')

    return wf


def create_spatial_map_dataflow(spatial_maps, wf_name='datasource_maps'):

    import os

    wf = pe.Workflow(name=wf_name)
    
    spatial_map_dict = {}
    
    for spatial_map_file in spatial_maps:

        spatial_map_file = spatial_map_file.rstrip('\r\n')
        base_file = os.path.basename(spatial_map_file)

        try:
            valid_extensions = ['.nii', '.nii.gz']

            base_name = [
                base_file[:-len(ext)]
                for ext in valid_extensions
                if base_file.endswith(ext)
            ][0]

            if base_name in spatial_map_dict:
                raise ValueError(
                    'Files with same name not allowed: %s %s' % (
                        spatial_map_file,
                        spatial_map_dict[base_name]
                    )
                )
        
            spatial_map_dict[base_name] = spatial_map_file
        
        except IndexError as e:
            raise Exception('Error in spatial_map_dataflow: '
                            'File extension not in .nii and .nii.gz')

    inputnode = pe.Node(util.IdentityInterface(fields=['spatial_map',
                                                       'spatial_map_file',
                                                       'creds_path',
                                                       'dl_dir'],
                                               mandatory_inputs=True),
                        name='inputspec')

    spatial_map_keys, spatial_map_values = \
        zip(*spatial_map_dict.items())

    inputnode.synchronize = True
    inputnode.iterables = [
        ('spatial_map', spatial_map_keys),
        ('spatial_map_file', spatial_map_values),
    ]

    check_s3_node = pe.Node(function.Function(input_names=['file_path',
                                                           'creds_path',
                                                           'dl_dir',
                                                           'img_type'],
                                              output_names=['local_path'],
                                              function=check_for_s3,
                                              as_module=True),
                            name='check_for_s3')

    wf.connect(inputnode, 'spatial_map_file', check_s3_node, 'file_path')
    wf.connect(inputnode, 'creds_path', check_s3_node, 'creds_path')
    wf.connect(inputnode, 'dl_dir', check_s3_node, 'dl_dir')
    check_s3_node.inputs.img_type = 'mask'

    select_spatial_map = pe.Node(util.IdentityInterface(fields=['out_file'],
                                                        mandatory_inputs=True),
                                 name='select_spatial_map')

    wf.connect(check_s3_node, 'local_path', select_spatial_map, 'out_file')

    return wf


def create_grp_analysis_dataflow(wf_name='gp_dataflow'):

    import nipype.pipeline.engine as pe
    import nipype.interfaces.utility as util
    from CPAC.utils.datasource import select_model_files

    wf = pe.Workflow(name=wf_name)

    inputnode = pe.Node(util.IdentityInterface(fields=['ftest',
                                                        'grp_model',
                                                        'model_name'],
                                                mandatory_inputs=True),
                        name='inputspec')

    selectmodel = pe.Node(function.Function(input_names=['model',
                                                         'ftest',
                                                         'model_name'],
                                            output_names=['fts_file',
                                                          'con_file',
                                                          'grp_file',
                                                          'mat_file'],
                                            function=select_model_files,
                                            as_module=True),
                          name='selectnode')

    wf.connect(inputnode, 'ftest',
                selectmodel, 'ftest')
    wf.connect(inputnode, 'grp_model',
                selectmodel, 'model')
    wf.connect(inputnode, 'model_name', selectmodel, 'model_name')

    outputnode = pe.Node(util.IdentityInterface(fields=['fts',
                                                        'grp',
                                                        'mat',
                                                        'con'],
                                                mandatory_inputs=True),
                            name='outputspec')

    wf.connect(selectmodel, 'mat_file',
                outputnode, 'mat')
    wf.connect(selectmodel, 'grp_file',
                outputnode, 'grp')
    wf.connect(selectmodel, 'fts_file',
                outputnode, 'fts')
    wf.connect(selectmodel, 'con_file',
                outputnode, 'con')

    return wf


def resample_func_roi(in_func, in_roi, realignment, identity_matrix):

    import os, subprocess
    import nibabel as nb    

    # load func and ROI dimension
    func_img = nb.load(in_func)
    func_shape = func_img.shape 
    roi_img = nb.load(in_roi)
    roi_shape = roi_img.shape 

    # check if func size = ROI size, return func and ROI; else resample using flirt 
    if roi_shape != func_shape:

        # resample func to ROI: in_file = func, reference = ROI
        if 'func_to_ROI' in realignment: 
            in_file = in_func
            reference = in_roi
            out_file = os.path.join(os.getcwd(), in_file[in_file.rindex('/')+1:in_file.rindex('.nii')]+'_resampled.nii.gz')
            out_func = out_file
            out_roi = in_roi
            interp = 'trilinear'

        # resample ROI to func: in_file = ROI, reference = func
        elif 'ROI_to_func' in realignment: 
            in_file = in_roi
            reference = in_func
            out_file = os.path.join(os.getcwd(), in_file[in_file.rindex('/')+1:in_file.rindex('.nii')]+'_resampled.nii.gz')
            out_func = in_func
            out_roi = out_file
            interp = 'nearestneighbour'

        cmd = ['flirt', '-in', in_file, 
                '-ref', reference, 
                '-out', out_file, 
                '-interp', interp, 
                '-applyxfm', '-init', identity_matrix]
        subprocess.check_output(cmd)

    else:
        out_func = in_func
        out_roi = in_roi
    
    return out_func, out_roi