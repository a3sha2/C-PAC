# -*- coding: utf-8 -*-
import os
import copy
import time
import shutil

from nipype import config
from nipype import logging
import nipype.pipeline.engine as pe
import nipype.interfaces.afni as afni
import nipype.interfaces.io as nio
from nipype.interfaces.utility import Merge, IdentityInterface

from indi_aws import aws_utils

from CPAC.utils.interfaces.datasink import DataSink
from CPAC.utils.interfaces.function import Function

import CPAC

from CPAC.registration import (
    create_fsl_flirt_linear_reg,
    create_fsl_fnirt_nonlinear_reg,
    create_register_func_to_anat,
    create_bbregister_func_to_anat,
    create_wf_calculate_ants_warp
)

from CPAC.utils.datasource import (
    resolve_resolution,
    create_anat_datasource,
    create_func_datasource,
    create_check_for_s3_node
)

from CPAC.anat_preproc.anat_preproc import create_anat_preproc
from CPAC.func_preproc.func_preproc import (
    create_func_preproc,
    create_wf_edit_func
)
from CPAC.longitudinal_pipeline.longitudinal_preproc import subject_specific_template

from CPAC.utils import Strategy, find_files, function, Outputs

from CPAC.utils.utils import (
    check_config_resources,
    check_system_deps,
    get_scan_params,
    get_tr
)

logger = logging.getLogger('nipype.workflow')


def func_longitudinal_workflow(sub_list, config):
    """
    This has to be completely reworked, it was just written long ago.

    """
    wf_list = []
    datasink = pe.Node(nio.DataSink(), name='sinker')
    datasink.inputs.base_directory = config.workingDirectory
    for sub_dict in sub_list:
        if 'func' in sub_dict:
            # for run in sub_dict['func']:
            """
            truncate
            (Func_preproc){
            two step motion corr 
            refit 
            resample
            motion corr
            skullstripping
            mean + median
            }  
            dist corr and apply dist corr res
            config file registration target (epi t1)
            """

            func_paths_dict = sub_dict['func']
            subject_id = sub_dict['subject_id']
            try:
                creds_path = sub_dict['creds_path']
                if creds_path and 'none' not in creds_path.lower():
                    if os.path.exists(creds_path):
                        input_creds_path = os.path.abspath(creds_path)
                    else:
                        err_msg = 'Credentials path: "%s" for subject "%s" was not ' \
                                  'found. Check this path and try again.' % (
                                      creds_path, subject_id)
                        raise Exception(err_msg)
                else:
                    input_creds_path = None
            except KeyError:
                input_creds_path = None

            func_wf = create_func_datasource(func_paths_dict,
                                             'func_gather_%s' % str(subject_id))
            func_wf.inputs.inputnode.set(
                subject=subject_id,
                creds_path=input_creds_path,
                dl_dir=config.workingDirectory
            )
            func_wf.get_node('inputnode').iterables = \
                ("scan", func_paths_dict.keys())

            # Add in nodes to get parameters from configuration file
            # a node which checks if scan_parameters are present for each scan
            scan_params = \
                pe.Node(
                    function.Function(input_names=['data_config_scan_params',
                                                   'subject_id',
                                                   'scan',
                                                   'pipeconfig_tr',
                                                   'pipeconfig_tpattern',
                                                   'pipeconfig_start_indx',
                                                   'pipeconfig_stop_indx'],
                                      output_names=['tr',
                                                    'tpattern',
                                                    'ref_slice',
                                                    'start_indx',
                                                    'stop_indx'],
                                      function=get_scan_params,
                                      as_module=True),
                    name='scan_params_%s' % str(subject_id))

            workflow_name = 'resting_preproc_' + str(subject_id)
            workflow = pe.Workflow(name=workflow_name)
            workflow.base_dir = config.workingDirectory
            workflow.config['execution'] = {
                'hash_method': 'timestamp',
                'crashdump_dir': os.path.abspath(config.crashLogDirectory)
            }

            if "Selected Functional Volume" in config.func_reg_input:
                get_func_volume = pe.Node(interface=afni.Calc(),
                                          name='get_func_volume_%s' % str(
                                              subject_id))

                get_func_volume.inputs.set(
                    expr='a',
                    single_idx=config.func_reg_input_volume,
                    outputtype='NIFTI_GZ'
                )
                workflow.connect(func_wf, 'outputspec.rest',
                                 get_func_volume, 'in_file_a')

            # wire in the scan parameter workflow
            workflow.connect(func_wf, 'outputspec.scan_params',
                             scan_params, 'data_config_scan_params')

            workflow.connect(func_wf, 'outputspec.subject',
                             scan_params, 'subject_id')

            workflow.connect(func_wf, 'outputspec.scan',
                             scan_params, 'scan')

            # connect in constants
            scan_params.inputs.set(
                pipeconfig_tr=config.TR,
                pipeconfig_tpattern=config.slice_timing_pattern,
                pipeconfig_start_indx=config.startIdx,
                pipeconfig_stop_indx=config.stopIdx
            )

            # node to convert TR between seconds and milliseconds
            convert_tr = pe.Node(function.Function(input_names=['tr'],
                                                   output_names=['tr'],
                                                   function=get_tr,
                                                   as_module=True),
                                 name='convert_tr_%s' % str(subject_id))

            # strat.update_resource_pool({
            #     'raw_functional': (func_wf, 'outputspec.rest'),
            #     'scan_id': (func_wf, 'outputspec.scan')
            # })

            trunc_wf = create_wf_edit_func(
                wf_name="edit_func_%s" % str(subject_id)
            )

            # connect the functional data from the leaf node into the wf
            workflow.connect(func_wf, 'outputspec.rest',
                             trunc_wf, 'inputspec.func')

            # connect the other input parameters
            workflow.connect(scan_params, 'start_indx',
                             trunc_wf, 'inputspec.start_idx')
            workflow.connect(scan_params, 'stop_indx',
                             trunc_wf, 'inputspec.stop_idx')

        # replace the leaf node with the output from the recently added
        # workflow
        # strat.set_leaf_properties(trunc_wf, 'outputspec.edited_func')

            # Functional Image Preprocessing Workflow
            if 1 in config.gen_custom_template:
                method = 'median'
            else:
                method = 'mean'

            if isinstance(config.functionalMasking, list):
                # For now, we just skullstrip using the first method selected
                func_masking = config.functionalMasking[0]
            else:
                func_masking = config.functionalMasking
            print(str(func_masking))
            if func_masking == '3dAutoMask':
                func_preproc = create_func_preproc(
                    use_bet=False,
                    method=method,
                    wf_name='func_preproc_automask_%s' % str(subject_id)
                )

                workflow.connect(trunc_wf, 'outputspec.edited_func',
                                 func_preproc, 'inputspec.func')

                func_preproc.inputs.inputspec.twopass = \
                    getattr(config, 'functional_volreg_twopass', True)

            elif func_masking == 'BET':
                func_preproc = create_func_preproc(use_bet=True,
                                                   method=method,
                                                   wf_name='func_preproc_bet_%s' % str(subject_id))

                workflow.connect(trunc_wf, 'outputspec.edited_func',
                                 func_preproc, 'inputspec.func')

                func_preproc.inputs.inputspec.twopass = \
                    getattr(config, 'functional_volreg_twopass', True)
            else:
                raise ValueError("functional masking method unsupported: " + str(func_masking))

            # workflow.connect(func_preproc, 'outputspec.preprocessed', datasink,
            #                  'preproc_func')

            wf_list.append(workflow)

            # func_preproc, 'outputspec.preprocessed'

    print("DOOOOOONE")
    return wf_list


def register_to_standard_template(long_reg_template_node, c, workflow):
    already_skullstripped = c.already_skullstripped[0]
    if already_skullstripped == 2:
        already_skullstripped = 0
    elif already_skullstripped == 3:
        already_skullstripped = 1

    sub_mem_gb, num_cores_per_sub, num_ants_cores = \
        check_config_resources(c)
    strat_initial = Strategy()

    strat_initial.update_resource_pool({
        'anatomical_brain': (long_reg_template_node, 'template')
    })

    templates_for_resampling = [
        (c.resolution_for_anat, c.template_brain_only_for_anat, 'template_brain_for_anat', 'resolution_for_anat'),
        (c.resolution_for_anat, c.template_skull_for_anat, 'template_skull_for_anat', 'resolution_for_anat'),
        (c.resolution_for_anat, c.template_symmetric_brain_only, 'template_symmetric_brain', 'resolution_for_anat'),
        (c.resolution_for_anat, c.template_symmetric_skull, 'template_symmetric_skull', 'resolution_for_anat'),
        (c.resolution_for_anat, c.dilated_symmetric_brain_mask, 'template_dilated_symmetric_brain_mask',
         'resolution_for_anat'),
        (c.resolution_for_anat, c.ref_mask, 'template_ref_mask', 'resolution_for_anat'),
        (c.resolution_for_func_preproc, c.template_brain_only_for_func, 'template_brain_for_func_preproc',
         'resolution_for_func_preproc'),
        (c.resolution_for_func_preproc, c.template_skull_for_func, 'template_skull_for_func_preproc',
         'resolution_for_func_preproc'),
        (c.resolution_for_func_derivative, c.template_brain_only_for_func, 'template_brain_for_func_derivative',
         'resolution_for_func_preproc'),
        (c.resolution_for_func_derivative, c.template_skull_for_func, 'template_skull_for_func_derivative',
         'resolution_for_func_preproc')
    ]

    # update resampled template to resource pool
    for resolution, template, template_name, tag in templates_for_resampling:
        # print(resolution, template, template_name)

        resampled_template = pe.Node(Function(input_names=['resolution', 'template', 'template_name', 'tag'],
                                              output_names=['resampled_template'],
                                              function=resolve_resolution,
                                              as_module=True),
                                     name='resampled_' + template_name)

        resampled_template.inputs.resolution = resolution
        resampled_template.inputs.template = template
        resampled_template.inputs.template_name = template_name
        resampled_template.inputs.tag = tag

        strat_initial.update_resource_pool({template_name: (resampled_template, 'resampled_template')})

    # node, out_file = strat['anatomical_brain']
    #
    # # pass the reference files
    # node, out_file = strat['template_brain_for_anat']
    #
    # node, out_file = strat['anatomical_reorient']
    #
    # node, out_file = strat['anatomical_to_mni_linear_xfm']
    #
    # node, out_file = strat['template_skull_for_anat']
    #
    # node, out_file = strat['template_symmetric_skull']
    #
    # node, out_file = strat['template_dilated_symmetric_brain_mask']
    #
    # node, out_file = strat['template_ref_mask']

    strat_list = [strat_initial]

    new_strat_list = []

    # either run FSL anatomical-to-MNI registration, or...
    if 'FSL' in c.regOption:
        for num_strat, strat in enumerate(strat_list):

            # this is to prevent the user from running FNIRT if they are
            # providing already-skullstripped inputs. this is because
            # FNIRT requires an input with the skull still on
            if already_skullstripped == 1:
                err_msg = '\n\n[!] CPAC says: FNIRT (for anatomical ' \
                          'registration) will not work properly if you ' \
                          'are providing inputs that have already been ' \
                          'skull-stripped.\n\nEither switch to using ' \
                          'ANTS for registration or provide input ' \
                          'images that have not been already ' \
                          'skull-stripped.\n\n'

                logger.info(err_msg)
                raise Exception

            flirt_reg_anat_mni = create_fsl_flirt_linear_reg(
                'anat_mni_flirt_register_%d' % num_strat
            )

            # if someone doesn't have anatRegFSLinterpolation in their pipe config,
            # it will default to sinc
            if not hasattr(c, 'anatRegFSLinterpolation'):
                setattr(c, 'anatRegFSLinterpolation', 'sinc')

            if c.anatRegFSLinterpolation not in ["trilinear", "sinc", "spline"]:
                err_msg = 'The selected FSL interpolation method may be in the list of values: "trilinear", "sinc", "spline"'
                raise Exception(err_msg)

            # Input registration parameters
            flirt_reg_anat_mni.inputs.inputspec.interp = c.anatRegFSLinterpolation

            node, out_file = strat['anatomical_brain']
            workflow.connect(node, out_file,
                             flirt_reg_anat_mni, 'inputspec.input_brain')

            # pass the reference files
            node, out_file = strat['template_brain_for_anat']
            workflow.connect(node, out_file,
                             flirt_reg_anat_mni, 'inputspec.reference_brain')

            if 'ANTS' in c.regOption:
                strat = strat.fork()
                new_strat_list.append(strat)

            strat.append_name(flirt_reg_anat_mni.name)
            strat.set_leaf_properties(flirt_reg_anat_mni,
                                      'outputspec.output_brain')

            strat.update_resource_pool({
                'anatomical_to_mni_linear_xfm': (flirt_reg_anat_mni, 'outputspec.linear_xfm'),
                'mni_to_anatomical_linear_xfm': (flirt_reg_anat_mni, 'outputspec.invlinear_xfm'),
                'anatomical_to_standard': (flirt_reg_anat_mni, 'outputspec.output_brain')
            })

    strat_list += new_strat_list

    new_strat_list = []

    try:
        fsl_linear_reg_only = c.fsl_linear_reg_only
    except AttributeError:
        fsl_linear_reg_only = [0]

    if 'FSL' in c.regOption and 0 in fsl_linear_reg_only:

        for num_strat, strat in enumerate(strat_list):

            nodes = strat.get_nodes_names()

            if 'anat_mni_flirt_register' in nodes:

                fnirt_reg_anat_mni = create_fsl_fnirt_nonlinear_reg(
                    'anat_mni_fnirt_register_%d' % num_strat
                )

                node, out_file = strat['anatomical_brain']
                workflow.connect(node, out_file,
                                 fnirt_reg_anat_mni, 'inputspec.input_brain')

                # pass the reference files
                node, out_file = strat['template_brain_for_anat']
                workflow.connect(node, out_file,
                                 fnirt_reg_anat_mni, 'inputspec.reference_brain')

                # We don't have this image for the longitudinal template
                # node, out_file = strat['anatomical_reorient']
                # workflow.connect(node, out_file,
                #                  fnirt_reg_anat_mni, 'inputspec.input_skull')

                node, out_file = strat['anatomical_to_mni_linear_xfm']
                workflow.connect(node, out_file,
                                 fnirt_reg_anat_mni, 'inputspec.linear_aff')

                node, out_file = strat['template_skull_for_anat']
                workflow.connect(node, out_file,
                                 fnirt_reg_anat_mni, 'inputspec.reference_skull')

                node, out_file = strat['template_ref_mask']
                workflow.connect(node, out_file,
                                 fnirt_reg_anat_mni, 'inputspec.ref_mask')

                # assign the FSL FNIRT config file specified in pipeline
                # config.yml
                fnirt_reg_anat_mni.inputs.inputspec.fnirt_config = c.fnirtConfig

                if 1 in fsl_linear_reg_only:
                    strat = strat.fork()
                    new_strat_list.append(strat)

                strat.append_name(fnirt_reg_anat_mni.name)
                strat.set_leaf_properties(fnirt_reg_anat_mni,
                                          'outputspec.output_brain')

                strat.update_resource_pool({
                    'anatomical_to_mni_nonlinear_xfm': (fnirt_reg_anat_mni, 'outputspec.nonlinear_xfm'),
                    'anatomical_to_standard': (fnirt_reg_anat_mni, 'outputspec.output_brain')
                }, override=True)

    strat_list += new_strat_list

    new_strat_list = []

    for num_strat, strat in enumerate(strat_list):

        nodes = strat.get_nodes_names()

        # or run ANTS anatomical-to-MNI registration instead
        if 'ANTS' in c.regOption and \
                'anat_mni_flirt_register' not in nodes and \
                'anat_mni_fnirt_register' not in nodes:

            ants_reg_anat_mni = \
                create_wf_calculate_ants_warp(
                    'anat_mni_ants_register_%d' % num_strat,
                    num_threads=num_ants_cores,
                    reg_ants_skull=c.regWithSkull
                )

            # if someone doesn't have anatRegANTSinterpolation in their pipe config,
            # it will default to LanczosWindowedSinc
            if not hasattr(c, 'anatRegANTSinterpolation'):
                setattr(c, 'anatRegANTSinterpolation', 'LanczosWindowedSinc')

            if c.anatRegANTSinterpolation not in ['Linear', 'BSpline', 'LanczosWindowedSinc']:
                err_msg = 'The selected ANTS interpolation method may be in the list of values: "Linear", "BSpline", "LanczosWindowedSinc"'
                raise Exception(err_msg)

            # Input registration parameters
            ants_reg_anat_mni.inputs.inputspec.interp = c.anatRegANTSinterpolation

            # calculating the transform with the skullstripped is
            # reported to be better, but it requires very high
            # quality skullstripping. If skullstripping is imprecise
            # registration with skull is preferred

            # TODO ASH assess with schema validator
            if 1 in c.regWithSkull:

                if already_skullstripped == 1:
                    err_msg = '\n\n[!] CPAC says: You selected ' \
                              'to run anatomical registration with ' \
                              'the skull, but you also selected to ' \
                              'use already-skullstripped images as ' \
                              'your inputs. This can be changed ' \
                              'in your pipeline configuration ' \
                              'editor.\n\n'

                    logger.info(err_msg)
                    raise Exception

                # get the skull-stripped anatomical from resource pool
                node, out_file = strat['anatomical_brain']

                # pass the anatomical to the workflow
                workflow.connect(node, out_file,
                                ants_reg_anat_mni, 'inputspec.moving_brain')

                # # get the reorient skull-on anatomical from resource pool
                # node, out_file = strat['anatomical_reorient']

                # # pass the anatomical to the workflow
                # workflow.connect(node, out_file,
                #                 ants_reg_anat_mni, 'inputspec.moving_skull')

                # pass the reference file
                node, out_file = strat['template_brain_for_anat']
                workflow.connect(node, out_file,
                                ants_reg_anat_mni, 'inputspec.reference_brain')

                # pass the reference file
                node, out_file = strat['template_skull_for_anat']
                workflow.connect(node, out_file,
                                ants_reg_anat_mni, 'inputspec.reference_skull')

            else:

                node, out_file = strat['anatomical_brain']

                workflow.connect(node, out_file, 
                                ants_reg_anat_mni, 'inputspec.moving_brain')

                # pass the reference file
                node, out_file = strat['template_brain_for_anat']
                workflow.connect(node, out_file,
                                ants_reg_anat_mni, 'inputspec.reference_brain')

            ants_reg_anat_mni.inputs.inputspec.ants_para = c.ANTs_para_T1_registration
            ants_reg_anat_mni.inputs.inputspec.fixed_image_mask = None

            strat.append_name(ants_reg_anat_mni.name)

            strat.set_leaf_properties(ants_reg_anat_mni,
                                      'outputspec.normalized_output_brain')

            strat.update_resource_pool({
                'ants_initial_xfm': (ants_reg_anat_mni, 'outputspec.ants_initial_xfm'),
                'ants_rigid_xfm': (ants_reg_anat_mni, 'outputspec.ants_rigid_xfm'),
                'ants_affine_xfm': (ants_reg_anat_mni, 'outputspec.ants_affine_xfm'),
                'anatomical_to_mni_nonlinear_xfm': (ants_reg_anat_mni, 'outputspec.warp_field'),
                'mni_to_anatomical_nonlinear_xfm': (ants_reg_anat_mni, 'outputspec.inverse_warp_field'),
                'anat_to_mni_ants_composite_xfm': (ants_reg_anat_mni, 'outputspec.composite_transform'),
                'anatomical_to_standard': (ants_reg_anat_mni, 'outputspec.normalized_output_brain')
            })

    strat_list += new_strat_list

    # [SYMMETRIC] T1 -> Symmetric Template, Non-linear registration (FNIRT/ANTS)

    new_strat_list = []

    if 1 in c.runVMHC and 1 in getattr(c, 'runFunctional', [1]):

        for num_strat, strat in enumerate(strat_list):

            nodes = strat.get_nodes_names()

            if 'FSL' in c.regOption and \
                    'anat_mni_ants_register' not in nodes:

                # this is to prevent the user from running FNIRT if they are
                # providing already-skullstripped inputs. this is because
                # FNIRT requires an input with the skull still on
                # TODO ASH normalize w schema validation to bool
                if already_skullstripped == 1:
                    err_msg = '\n\n[!] CPAC says: FNIRT (for anatomical ' \
                              'registration) will not work properly if you ' \
                              'are providing inputs that have already been ' \
                              'skull-stripped.\n\nEither switch to using ' \
                              'ANTS for registration or provide input ' \
                              'images that have not been already ' \
                              'skull-stripped.\n\n'

                    logger.info(err_msg)
                    raise Exception

                flirt_reg_anat_symm_mni = create_fsl_flirt_linear_reg(
                    'anat_symmetric_mni_flirt_register_%d' % num_strat
                )

                # Input registration parameters
                flirt_reg_anat_symm_mni.inputs.inputspec.interp = c.anatRegFSLinterpolation

                node, out_file = strat['anatomical_brain']
                workflow.connect(node, out_file,
                                 flirt_reg_anat_symm_mni, 'inputspec.input_brain')

                # pass the reference files
                node, out_file = strat['template_symmetric_brain']
                workflow.connect(node, out_file,
                                 flirt_reg_anat_symm_mni, 'inputspec.reference_brain')

                # if 'ANTS' in c.regOption:
                #    strat = strat.fork()
                #    new_strat_list.append(strat)

                strat.append_name(flirt_reg_anat_symm_mni.name)
                strat.set_leaf_properties(flirt_reg_anat_symm_mni,
                                          'outputspec.output_brain')

                strat.update_resource_pool({
                    'anatomical_to_symmetric_mni_linear_xfm': (
                        flirt_reg_anat_symm_mni, 'outputspec.linear_xfm'),
                    'symmetric_mni_to_anatomical_linear_xfm': (
                        flirt_reg_anat_symm_mni, 'outputspec.invlinear_xfm'),
                    'symmetric_anatomical_to_standard': (
                        flirt_reg_anat_symm_mni, 'outputspec.output_brain')
                })

        strat_list += new_strat_list

        new_strat_list = []

        try:
            fsl_linear_reg_only = c.fsl_linear_reg_only
        except AttributeError:
            fsl_linear_reg_only = [0]

        if 'FSL' in c.regOption and 0 in fsl_linear_reg_only:

            for num_strat, strat in enumerate(strat_list):

                nodes = strat.get_nodes_names()

                if 'anat_mni_flirt_register' in nodes:
                    fnirt_reg_anat_symm_mni = create_fsl_fnirt_nonlinear_reg(
                        'anat_symmetric_mni_fnirt_register_%d' % num_strat
                    )

                    node, out_file = strat['anatomical_brain']
                    workflow.connect(node, out_file,
                                     fnirt_reg_anat_symm_mni,
                                     'inputspec.input_brain')

                    # pass the reference files
                    node, out_file = strat['template_brain_for_anat']
                    workflow.connect(node, out_file,
                                     fnirt_reg_anat_symm_mni, 'inputspec.reference_brain')

                    # node, out_file = strat['anatomical_reorient']
                    # workflow.connect(node, out_file,
                    #                  fnirt_reg_anat_symm_mni,
                    #                  'inputspec.input_skull')

                    node, out_file = strat['anatomical_to_mni_linear_xfm']
                    workflow.connect(node, out_file,
                                     fnirt_reg_anat_symm_mni,
                                     'inputspec.linear_aff')

                    node, out_file = strat['template_symmetric_skull']
                    workflow.connect(node, out_file,
                                     fnirt_reg_anat_symm_mni, 'inputspec.reference_skull')

                    node, out_file = strat['template_dilated_symmetric_brain_mask']
                    workflow.connect(node, out_file,
                                     fnirt_reg_anat_symm_mni, 'inputspec.ref_mask')

                    strat.append_name(fnirt_reg_anat_symm_mni.name)
                    strat.set_leaf_properties(fnirt_reg_anat_symm_mni,
                                              'outputspec.output_brain')

                    strat.update_resource_pool({
                        'anatomical_to_symmetric_mni_nonlinear_xfm': (
                            fnirt_reg_anat_symm_mni, 'outputspec.nonlinear_xfm'),
                        'symmetric_anatomical_to_standard': (
                            fnirt_reg_anat_symm_mni, 'outputspec.output_brain')
                    }, override=True)

        strat_list += new_strat_list

        new_strat_list = []

        for num_strat, strat in enumerate(strat_list):

            nodes = strat.get_nodes_names()

            # or run ANTS anatomical-to-MNI registration instead
            if 'ANTS' in c.regOption and \
                    'anat_mni_flirt_register' not in nodes and \
                    'anat_mni_fnirt_register' not in nodes and \
                    'anat_symmetric_mni_flirt_register' not in nodes and \
                    'anat_symmetric_mni_fnirt_register' not in nodes:

                ants_reg_anat_symm_mni = \
                    create_wf_calculate_ants_warp(
                        'anat_symmetric_mni_ants_register_%d' % num_strat,
                        num_threads=num_ants_cores,
                        reg_ants_skull=c.regWithSkull
                    )

                # Input registration parameters
                ants_reg_anat_symm_mni.inputs.inputspec.interp = c.anatRegANTSinterpolation

                # calculating the transform with the skullstripped is
                # reported to be better, but it requires very high
                # quality skullstripping. If skullstripping is imprecise
                # registration with skull is preferred
                if 1 in c.regWithSkull:

                    if already_skullstripped == 1:
                        err_msg = '\n\n[!] CPAC says: You selected ' \
                                  'to run anatomical registration with ' \
                                  'the skull, but you also selected to ' \
                                  'use already-skullstripped images as ' \
                                  'your inputs. This can be changed ' \
                                  'in your pipeline configuration ' \
                                  'editor.\n\n'

                        logger.info(err_msg)
                        raise Exception

                    # get the skullstripped anatomical from resource pool
                    node, out_file = strat['anatomical_brain']

                    # pass the anatomical to the workflow
                    workflow.connect(node, out_file,
                                     ants_reg_anat_symm_mni, 'inputspec.moving_brain')

                    # pass the reference file
                    node, out_file = strat['template_symmetric_brain']
                    workflow.connect(node, out_file,
                                     ants_reg_anat_symm_mni, 'inputspec.reference_brain')

                    # TODO debug KeyError: 'anatomical_reorient' if regWithSkull: [1]
                    # # get the reorient skull-on anatomical from resource pool
                    # node, out_file = strat['anatomical_reorient']
                    
                    # # pass the anatomical to the workflow
                    # workflow.connect(node, out_file,
                    #                  ants_reg_anat_symm_mni, 'inputspec.anatomical_skull')

                    # pass the reference file
                    node, out_file = strat['template_symmetric_skull']
                    workflow.connect(node, out_file,
                                     ants_reg_anat_symm_mni, 'inputspec.reference_skull')

                else:
                    # get the skullstripped anatomical from resource pool
                    node, out_file = strat['anatomical_brain']

                    workflow.connect(node, out_file,
                                     ants_reg_anat_symm_mni, 'inputspec.moving_brain')

                    # pass the reference file
                    node, out_file = strat['template_symmetric_brain']
                    workflow.connect(node, out_file,
                                     ants_reg_anat_symm_mni, 'inputspec.reference_brain')

                ants_reg_anat_symm_mni.inputs.inputspec.ants_para = c.ANTs_para_T1_registration

                ants_reg_anat_symm_mni.inputs.inputspec.fixed_image_mask = None

                strat.append_name(ants_reg_anat_symm_mni.name)
                strat.set_leaf_properties(ants_reg_anat_symm_mni,
                                          'outputspec.normalized_output_brain')

                strat.update_resource_pool({
                    'ants_symmetric_initial_xfm': (ants_reg_anat_symm_mni, 'outputspec.ants_initial_xfm'),
                    'ants_symmetric_rigid_xfm': (ants_reg_anat_symm_mni, 'outputspec.ants_rigid_xfm'),
                    'ants_symmetric_affine_xfm': (ants_reg_anat_symm_mni, 'outputspec.ants_affine_xfm'),
                    'anatomical_to_symmetric_mni_nonlinear_xfm': (ants_reg_anat_symm_mni, 'outputspec.warp_field'),
                    'symmetric_mni_to_anatomical_nonlinear_xfm': (
                    ants_reg_anat_symm_mni, 'outputspec.inverse_warp_field'),
                    'anat_to_symmetric_mni_ants_composite_xfm': (
                    ants_reg_anat_symm_mni, 'outputspec.composite_transform'),
                    'symmetric_anatomical_to_standard': (ants_reg_anat_symm_mni, 'outputspec.normalized_output_brain')
                })

        strat_list += new_strat_list
    
    return strat_list


def create_datasink(datasink_name, config, subject_id, session_id='', strat_name='', map_node_iterfield=None):
    """

    Parameters
    ----------
    datasink_name
    config
    subject_id
    session_id
    strat_name
    map_node_iterfield

    Returns
    -------

    """
    try:
        encrypt_data = bool(config.s3Encryption[0])
    except:
        encrypt_data = False

    # TODO enforce value with schema validation
    # Extract credentials path for output if it exists
    try:
        # Get path to creds file
        creds_path = ''
        if config.awsOutputBucketCredentials:
            creds_path = str(config.awsOutputBucketCredentials)
            creds_path = os.path.abspath(creds_path)

        if config.outputDirectory.lower().startswith('s3://'):
            # Test for s3 write access
            s3_write_access = \
                aws_utils.test_bucket_access(creds_path,
                                             config.outputDirectory)

            if not s3_write_access:
                raise Exception('Not able to write to bucket!')

    except Exception as e:
        if config.outputDirectory.lower().startswith('s3://'):
            err_msg = 'There was an error processing credentials or ' \
                      'accessing the S3 bucket. Check and try again.\n' \
                      'Error: %s' % e
            raise Exception(err_msg)
    if map_node_iterfield is not None:
        ds = pe.MapNode(
            DataSink(infields=map_node_iterfield),
            name='sinker_{}'.format(datasink_name),
            iterfield=map_node_iterfield
        )
    else:
        ds = pe.Node(
            DataSink(),
            name='sinker_{}'.format(datasink_name)
        )
    ds.inputs.base_directory = config.outputDirectory
    ds.inputs.creds_path = creds_path
    ds.inputs.encrypt_bucket_keys = encrypt_data
    ds.inputs.container = os.path.join(
        'pipeline_%s_%s' % (config.pipelineName, strat_name),
        subject_id, session_id
    )
    return ds

def connect_anat_preproc_inputs(strat_in, anat_preproc_in, strat_name, strat_nodes_list_list, workflow):
    """
    Parameters
    ----------
    strat_in : Strategy
        the strategy object you want to fork
    anat_preproc_in : Workflow
        the anat_preproc workflow node to be connected and added to the resource pool
    strat_name : str
        name of the strategy
    strat_nodes_list_list : list
        a list of strat_nodes_list ???
    workflow: Workflow
        main longitudinal workflow

    Returns
    -------
    new_strat_out : Strategy
        the fork of strat_in with the resource pool updated
    strat_nodes_list_list : list
        a list of strat_nodes_list ???
    """

    new_strat_out = strat_in.fork()

    tmp_node, out_key = new_strat_out['anatomical']
    workflow.connect(tmp_node, out_key, anat_preproc_in, 'inputspec.anat')
    # import pdb; pdb.set_trace()
    tmp_node, out_key = new_strat_out['template_cmass']
    workflow.connect(tmp_node, out_key, anat_preproc_in, 'inputspec.template_cmass')

    new_strat_out.append_name(anat_preproc_in.name)
    # TODO it is not used further so I think it can be removed
    # new_strat_out.set_leaf_properties(anat_preproc_in, 'outputspec.brain')
    new_strat_out.update_resource_pool({
        'anatomical_brain': (
            anat_preproc_in, 'outputspec.brain'),
        'anatomical_reorient': (
            anat_preproc_in, 'outputspec.reorient'),
    })
    try:
        strat_nodes_list_list[strat_name].append(new_strat_out)
    except KeyError:
        strat_nodes_list_list[strat_name] = [new_strat_out]

    return new_strat_out, strat_nodes_list_list


def anat_longitudinal_workflow(sub_list, subject_id, config):
    """
    Parameters
    ----------
    sub_list : list of dict
        this is a list of sessions for one subject and each session if the same dictionary as the one given to
        prep_workflow
    subject_id : str
        the id of the subject
    config : Configuration
        a configuration object containing the information of the pipeline config. (Same as for prep_workflow)

    Returns
    -------
        None
        runs the workflow once it is built
    """

    workflow = pe.Workflow(name="longitudinal_template_" + str(subject_id))
    workflow.base_dir = config.workingDirectory

    # For each participant we have a list of dict (each dict is a session)
    already_skullstripped = config.already_skullstripped[0]
    if already_skullstripped == 2:
        already_skullstripped = 0
    elif already_skullstripped == 3:
        already_skullstripped = 1

    # template_skull_for_anat_path = resolve_resolution(
    #     config.resolution_for_anat,
    #     config.template_skull_for_anat,
    #     'template_skull_for_anat',
    #     'resolution_for_anat')

    resampled_template = pe.Node(Function(input_names=['resolution', 'template', 'template_name', 'tag'],
                                              output_names=['resampled_template'],
                                              function=resolve_resolution,
                                              as_module=True),
                                        name='template_skull_for_anat')
    resampled_template.inputs.resolution = config.resolution_for_anat
    resampled_template.inputs.template = config.template_skull_for_anat
    resampled_template.inputs.template_name = 'template_skull_for_anat'
    resampled_template.inputs.tag = 'resolution_for_anat'

    # Node to calculate the center of mass of the standard template to align the images with it.
    template_center_of_mass = pe.Node(
        interface=afni.CenterMass(),
        name='template_skull_for_anat_center_of_mass'
    )
    template_center_of_mass.inputs.cm_file = "template_center_of_mass.txt"
    # template_center_of_mass.inputs.in_file = template_skull_for_anat_path

    workflow.connect(resampled_template, 'resampled_template',
                     template_center_of_mass, 'in_file')

    # list of lists for every strategy
    strat_nodes_list_list = {}

    # list of the data config dictionaries to be updated during the preprocessing
    creds_list = []

    session_id_list = []
    # Loop over the sessions to create the input for the longitudinal algo
    for session in sub_list:
        unique_id = session['unique_id']
        session_id_list.append(unique_id)

        try:
            creds_path = session['creds_path']
            if creds_path and 'none' not in creds_path.lower():
                if os.path.exists(creds_path):
                    input_creds_path = os.path.abspath(creds_path)
                else:
                    err_msg = 'Credentials path: "%s" for subject "%s" session "%s" ' \
                              'was not found. Check this path and try ' \
                              'again.' % (creds_path, subject_id, unique_id)
                    raise Exception(err_msg)
            else:
                input_creds_path = None
        except KeyError:
            input_creds_path = None

        creds_list.append(input_creds_path)

        strat = Strategy()
        strat_list = []
        node_suffix = '_'.join([subject_id, unique_id])

        anat_rsc = create_anat_datasource(
            'anat_gather_%s' % node_suffix)
        anat_rsc.inputs.inputnode.subject = subject_id
        anat_rsc.inputs.inputnode.anat = session['anat']
        anat_rsc.inputs.inputnode.creds_path = input_creds_path
        anat_rsc.inputs.inputnode.dl_dir = config.workingDirectory

        strat.update_resource_pool({
            'anatomical': (anat_rsc, 'outputspec.anat')
        })

        strat.update_resource_pool({
            'template_cmass': (template_center_of_mass, 'cm')
        })

        # Here we have the same strategies for the skull stripping as in prep_workflow
        if 'brain_mask' in session.keys() and session['brain_mask'] and \
                session['brain_mask'].lower() != 'none':

            brain_rsc = create_anat_datasource(
                'brain_gather_%s' % unique_id)
            brain_rsc.inputs.inputnode.subject = subject_id
            brain_rsc.inputs.inputnode.anat = session['brain_mask']
            brain_rsc.inputs.inputnode.creds_path = input_creds_path
            brain_rsc.inputs.inputnode.dl_dir = config.workingDirectory

            skullstrip_method = 'mask'
            preproc_wf_name = 'anat_preproc_mask_%s' % node_suffix

            strat.append_name(brain_rsc.name)
            strat.update_resource_pool({
                'anatomical_brain_mask': (brain_rsc, 'outputspec.anat')
            })

            anat_preproc = create_anat_preproc(
                method=skullstrip_method,
                config=config,
                wf_name=preproc_wf_name)

            workflow.connect(brain_rsc, 'outputspec.brain_mask',
                             anat_preproc, 'inputspec.brain_mask')
            new_strat, strat_nodes_list_list = connect_anat_preproc_inputs(strat, anat_preproc, skullstrip_method + "_skullstrip", strat_nodes_list_list, workflow)
            strat_list.append(new_strat)

        elif already_skullstripped:
            skullstrip_method = None
            preproc_wf_name = 'anat_preproc_already_%s' % node_suffix
            anat_preproc = create_anat_preproc(
                method=skullstrip_method,
                already_skullstripped=True,
                config=config,
                wf_name=preproc_wf_name
            )
            new_strat, strat_nodes_list_list = connect_anat_preproc_inputs(strat, anat_preproc, 'already_skullstripped', strat_nodes_list_list, workflow)
            strat_list.append(new_strat)

        else:
            # TODO add other SS methods 
            if "AFNI" in config.skullstrip_option:
                skullstrip_method = 'afni'
                preproc_wf_name = 'anat_preproc_afni_%s' % node_suffix

                anat_preproc = create_anat_preproc(
                    method=skullstrip_method,
                    config=config,
                    wf_name=preproc_wf_name)

                anat_preproc.inputs.AFNI_options.set(
                    shrink_factor=config.skullstrip_shrink_factor,
                    var_shrink_fac=config.skullstrip_var_shrink_fac,
                    shrink_fac_bot_lim=config.skullstrip_shrink_factor_bot_lim,
                    avoid_vent=config.skullstrip_avoid_vent,
                    niter=config.skullstrip_n_iterations,
                    pushout=config.skullstrip_pushout,
                    touchup=config.skullstrip_touchup,
                    fill_hole=config.skullstrip_fill_hole,
                    avoid_eyes=config.skullstrip_avoid_eyes,
                    use_edge=config.skullstrip_use_edge,
                    exp_frac=config.skullstrip_exp_frac,
                    smooth_final=config.skullstrip_smooth_final,
                    push_to_edge=config.skullstrip_push_to_edge,
                    use_skull=config.skullstrip_use_skull,
                    perc_int=config.skullstrip_perc_int,
                    max_inter_iter=config.skullstrip_max_inter_iter,
                    blur_fwhm=config.skullstrip_blur_fwhm,
                    fac=config.skullstrip_fac,
                    monkey=config.skullstrip_monkey,
                    mask_vol=config.skullstrip_mask_vol
                )

                new_strat, strat_nodes_list_list = connect_anat_preproc_inputs(strat, anat_preproc, skullstrip_method + "_skullstrip", strat_nodes_list_list, workflow)
                strat_list.append(new_strat)

            if "BET" in config.skullstrip_option:
                skullstrip_method = 'fsl'
                preproc_wf_name = 'anat_preproc_fsl_%s' % node_suffix

                anat_preproc = create_anat_preproc(
                    method=skullstrip_method,
                    wf_name=preproc_wf_name,
                    non_local_means_filtering=config.non_local_means_filtering,
                    n4_correction=config.n4_bias_field_correction)

                anat_preproc.inputs.BET_options.set(
                    frac=config.bet_frac,
                    mask_boolean=config.bet_mask_boolean,
                    mesh_boolean=config.bet_mesh_boolean,
                    outline=config.bet_outline,
                    padding=config.bet_padding,
                    radius=config.bet_radius,
                    reduce_bias=config.bet_reduce_bias,
                    remove_eyes=config.bet_remove_eyes,
                    robust=config.bet_robust,
                    skull=config.bet_skull,
                    surfaces=config.bet_surfaces,
                    threshold=config.bet_threshold,
                    vertical_gradient=config.bet_vertical_gradient,
                )

                new_strat, strat_nodes_list_list = connect_anat_preproc_inputs(strat, anat_preproc, skullstrip_method + "_skullstrip", strat_nodes_list_list, workflow)
                strat_list.append(new_strat)

            if not any(o in config.skullstrip_option for o in
                       ["AFNI", "BET"]):
                err = '\n\n[!] C-PAC says: Your skull-stripping ' \
                      'method options setting does not include either' \
                      ' \'AFNI\' or \'BET\'.\n\n Options you ' \
                      'provided:\nskullstrip_option: {0}\n\n'.format(
                        str(config.skullstrip_option))
                raise Exception(err)

    # Here we have all the anat_preproc set up for every session of the subject

    # loop over the different skull stripping strategies
    for strat_name, strat_nodes_list in strat_nodes_list_list.items():
        node_suffix = '_'.join([strat_name, subject_id])
        # Merge node to feed the anat_preproc outputs to the longitudinal template generation
        merge_node = pe.Node(
            interface=Merge(len(strat_nodes_list)),
            name="anat_longitudinal_merge_" + node_suffix)

        # This node will generate the longitudinal template (the functions are in longitudinal_preproc)
        # Later other algorithms could be added to calculate it, like the multivariate template from ANTS
        # It would just require to change it here.
        template_node = subject_specific_template(
            workflow_name='subject_specific_template_' + node_suffix
        )
        # template_node.inputs.output_folder = os.getcwd()
        template_node.inputs.set(
            avg_method=config.long_reg_avg_method,
            dof=config.dof,
            interp=config.interp,
            cost=config.cost,
            convergence_threshold=config.convergence_threshold,
            thread_pool=config.thread_pool,
        )

        rsc_key = 'anat_longitudinal_template'
        ds_template = create_datasink(rsc_key + node_suffix, config, subject_id, strat_name='longitudinal_'+strat_name)
        workflow.connect(template_node, 'template', ds_template, rsc_key)

        rsc_key = 'subject_to_longitudinal_template_warp_'
        ds_warp_list = create_datasink(rsc_key + node_suffix, config, subject_id, strat_name='longitudinal_'+strat_name,
                                       map_node_iterfield=['warp_list'])
        workflow.connect(template_node, "final_warp_list", ds_warp_list, 'warp_list')

        # TODO debug the registration from the longitudinal template to the standard template (MNI?)
        reg_strat_list = register_to_standard_template(template_node, config, workflow)
        # import pdb;pdb.set_trace()
        for index, strat in enumerate(reg_strat_list):
            for rsc_key in strat.resource_pool.keys():
                rsc_nodes_suffix = 'long_reg_to_standard_' + str(index)
                if rsc_key in Outputs.any:
                    node, rsc_name = strat[rsc_key]
                    ds = create_datasink(rsc_key + rsc_nodes_suffix, config, subject_id, strat_name='longitudinal_'+strat_name)
                    workflow.connect(node, rsc_name, ds, rsc_key)

        # the in{}.format take i+1 because the Merge nodes inputs starts at 1 ...
        for i in range(len(strat_nodes_list)):
            rsc_nodes_suffix = "_%s_%d" % (node_suffix, i)
            for rsc_key in strat_nodes_list[i].resource_pool.keys():
                if rsc_key in Outputs.any:
                    node, rsc_name = strat_nodes_list[i][rsc_key]
                    ds = create_datasink(rsc_key + rsc_nodes_suffix, config, subject_id,
                                         session_id_list[i], 'longitudinal_'+strat_name)
                    workflow.connect(node, rsc_name, ds, rsc_key)
            rsc_key = 'anatomical_brain'
            anat_preproc_node, rsc_name = strat_nodes_list[i][rsc_key]
            workflow.connect(anat_preproc_node,
                             rsc_name, merge_node,
                             'in{}'.format(i + 1))

        workflow.connect(merge_node, 'out', template_node, 'img_list')

    workflow.run()

    return
