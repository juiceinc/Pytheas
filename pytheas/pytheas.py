import argparse
import codecs
import csv
import json
import os
import pprint
import re
import string
import subprocess
import sys
import traceback
from multiprocessing import Pool
from os import listdir
from os.path import isfile, join
from timeit import default_timer as timer

import evaluation.evaluation_utilities as evaluation_utilities
import numpy as np
import pandas as pd
from dotmap import DotMap
from sortedcontainers import SortedDict
from tqdm import tqdm

import pytheas.file_utilities as file_utilities
import pytheas.nb_utilities as nb_util
import pytheas.pat_utilities as pat_util
import pytheas.table_classifier_utilities as table_classifier_utilities
from pytheas.header_events import (
    collect_arithmetic_events_on_row,
    collect_events_on_row,
    header_row_with_aggregation_tokens,
)
from pytheas.table_classifier_utilities import (
    TableSignatures,
    all_numbers,
    assess_data_line,
    assess_non_data_line,
    contains_number,
    discover_aggregation_scope,
    eval_data_cell_rule,
    eval_not_data_cell_rule,
    is_consistent_symbol_sets,
    is_number,
    line_has_null_equivalent,
    name_table_columns,
    predict_combined_data_confidences,
    predict_fdl,
    predict_header_indexes,
    predict_line_label,
)


from .nb_utilities import stop
pp = pprint.PrettyPrinter(indent=4)


DEFAULT_WEIGHTS = object()


def generate_processing_tasks(
    pytheas_model, db_cred, files, max_lines, top_level_dir, opendata_engine
):
    file_counter = -1
    for file in files:
        file_counter += 1
        crawl_datafile_key, size_in_bytes, ground_truth_path, endpoint = file
        filepath = os.path.join(top_level_dir, ground_truth_path)
        task = (
            db_cred,
            file_counter,
            pytheas_model,
            crawl_datafile_key,
            endpoint,
            filepath,
            max_lines,
            opendata_engine,
        )
        yield task


def message_slack(message):
    pass


def available_cpu_count():
    """Number of available virtual or physical CPUs on this system, i.e.
    user/real as output by time(1) when called with an optimally scaling
    userspace-only program"""

    # cpuset
    # cpuset may restrict the number of *available* processors
    try:
        m = re.search(r"(?m)^Cpus_allowed:\s*(.*)$", open("/proc/self/status").read())
        if m:
            res = bin(int(m.group(1).replace(",", ""), 16)).count("1")
            if res > 0:
                return res
    except IOError:
        pass

    # Python 2.6+
    try:
        import multiprocessing

        return multiprocessing.cpu_count()
    except (ImportError, NotImplementedError):
        pass

    # https://github.com/giampaolo/psutil
    try:
        import psutil

        return psutil.cpu_count()  # psutil.NUM_CPUS on old versions
    except (ImportError, AttributeError):
        pass

    # POSIX
    try:
        res = int(os.sysconf("SC_NPROCESSORS_ONLN"))

        if res > 0:
            return res
    except (AttributeError, ValueError):
        pass

    # Windows
    try:
        res = int(os.environ["NUMBER_OF_PROCESSORS"])

        if res > 0:
            return res
    except (KeyError, ValueError):
        pass

    # jython
    try:
        from java.lang import Runtime

        runtime = Runtime.getRuntime()
        res = runtime.availableProcessors()
        if res > 0:
            return res
    except ImportError:
        pass

    # BSD
    try:
        sysctl = subprocess.Popen(["sysctl", "-n", "hw.ncpu"], stdout=subprocess.PIPE)
        scStdout = sysctl.communicate()[0]
        res = int(scStdout)

        if res > 0:
            return res
    except (OSError, ValueError):
        pass

    # Linux
    try:
        res = open("/proc/cpuinfo").read().count("processor\t:")

        if res > 0:
            return res
    except IOError:
        pass

    # Solaris
    try:
        pseudoDevices = os.listdir("/devices/pseudo/")
        res = 0
        for pd in pseudoDevices:
            if re.match(r"^cpuid@[0-9]+$", pd):
                res += 1

        if res > 0:
            return res
    except OSError:
        pass

    # Other UNIXes (heuristic)
    try:
        try:
            dmesg = open("/var/run/dmesg.boot").read()
        except IOError:
            dmesgProcess = subprocess.Popen(["dmesg"], stdout=subprocess.PIPE)
            dmesg = dmesgProcess.communicate()[0]

        res = 0
        while "\ncpu" + str(res) + ":" in dmesg:
            res += 1

        if res > 0:
            return res
    except OSError:
        pass

    raise Exception("Can not determine number of CPUs on this system")


class API(object):
    def __init__(self, weights=DEFAULT_WEIGHTS, db_params=None):
        self.real_pytheas = PYTHEAS()
        self.load_weights(weights)

    def load_weights(self, filepath):
        if filepath is None:
            pass
        elif filepath == DEFAULT_WEIGHTS:
            self.real_pytheas.load_default_weights()
        else:
            self.real_pytheas.load_weights(filepath)

    def infer_annotations(self, filepath, max_lines=None):
        return self.real_pytheas.infer_annotations(filepath, max_lines)

    def infer_annotations_from_df(self, df):
        return self.real_pytheas.infer_annotations_from_df(df)

    def learn_and_save_weights(
        self,
        files_path,
        annotations_path,
        output_path="train_output.json",
        parameters=None,
    ):
        # self.real_pytheas.clear_weights()
        if parameters:
            self.real_pytheas.set_params(parameters)

        files = sorted(
            [
                os.path.join(files_path, f)
                for f in listdir(files_path)
                if isfile(join(files_path, f))
            ]
        )
        annotations = sorted(
            [
                os.path.join(annotations_path, f)
                for f in listdir(annotations_path)
                if isfile(join(annotations_path, f))
            ]
        )

        NINPUTS = len(files)
        NPROC = available_cpu_count()
        print(f"NINPUTS={NINPUTS}")
        print(f"NPROC={NPROC}")
        # Process files
        combined_data = []
        with Pool(processes=NPROC) as pool:
            with tqdm(total=NINPUTS) as pbar:
                for r in pool.imap_unordered(
                    self.real_pytheas.rules_fired_in_file,
                    [
                        (key, f[0], f[1])
                        for key, f in enumerate(zip(files, annotations))
                    ],
                ):
                    if isinstance(r, Exception):
                        print("Got exception: {}".format(r))
                    else:
                        combined_data.append(r)
                    pbar.update(1)

        # for task in [ (key, f[0], f[1])  for key,f in enumerate(zip(files,annotations))]:
        #     pp.pprint(task)
        #     r = self.real_pytheas.rules_fired_in_file(task)
        #     if isinstance(r, Exception):
        #         print("Got exception: {}".format(r))
        #     else:
        #         combined_data.append(r)
        #     input()

        print(f"{len(combined_data)} files successfully processed.")

        print("####### REDUCE ######")
        start_reduce = timer()
        pat_line_datapoints_DATA = []
        pat_cell_datapoints_DATA = []
        pat_data_line_rules_DATA = []
        pat_not_data_line_rules_DATA = []
        pat_data_cell_rules_DATA = []
        pat_not_data_cell_rules_DATA = []
        pat_line_and_cell_rules_DATA = []
        for res in combined_data:
            datafile_key = res[0]
            pat_line_datapoints_DATA.append(res[1])
            pat_cell_datapoints_DATA.append(res[2])
            pat_data_line_rules_DATA.append(res[3])
            pat_not_data_line_rules_DATA.append(res[4])
            pat_data_cell_rules_DATA.append(res[5])
            pat_not_data_cell_rules_DATA.append(res[6])
            data_rules_fired = Json(res[7])
            not_data_rules_fired = Json(res[8])
            pat_line_and_cell_rules_DATA.append(
                (datafile_key, data_rules_fired, not_data_rules_fired)
            )

        pat_data_line_rules = pd.concat(pat_data_line_rules_DATA)
        pat_not_data_line_rules = pd.concat(pat_not_data_line_rules_DATA)
        pat_data_cell_rules = pd.concat(pat_data_cell_rules_DATA)
        pat_not_data_cell_rules = pd.concat(pat_not_data_cell_rules_DATA)

        undersampled_cell_data = pat_data_cell_rules.query("undersample==True")
        undersampled_cell_not_data = pat_not_data_cell_rules.query("undersample==True")
        undersampled_line_data = pat_data_line_rules.query("undersample==True")
        undersampled_line_not_data = pat_not_data_line_rules.query("undersample==True")

        self.real_pytheas.train_rules(
            undersampled_cell_data,
            undersampled_cell_not_data,
            undersampled_line_data,
            undersampled_line_not_data,
        )

        output = dict(
            fuzzy_rules=self.real_pytheas.fuzzy_rules,
            parameters=self.real_pytheas.parameters,
        )
        with open(output_path, "w") as outfile:
            json.dump(output, outfile)


class PYTHEAS:
    def __init__(self):
        self.parameters = DotMap(
            {
                "undersample_data_limit": 2,
                "max_candidates": 100,
                "max_summary_strength": 6,  # maximum non-empty values to consider for context
                "max_line_depth": 30,  # max depth at which to search for the first data line
                "max_attributes": 20,  # cuttof for columns to be considered (from left to right) when collecting class confidence
                "outlier_sensitive": True,
                "normalize_decimals": True,
                "impute_nulls": True,
                "ignore_left": 4,  # if there are enough columns, ignore the first ignore_left when evaluating column class confidence (avoids taking into account what is often referred to as index columns or left headers.)
                "summary_population_factor": True,
                "weight_input": "values_and_lines",
                "weight_lower_bound": 0.4,
                "not_data_weight_lower_bound": 0.6,
                "p": 0.3,
                "markov_model": None,
                "markov_approximation_probabilities": None,
                "combined_label_weight": "confidence",  # one of [confidence,confusion_index,difference]
            }
        )
        self.ignore_rules = {
            "cell": {"not_data": [], "data": []},
            "line": {"not_data": [], "data": []},
        }
        self.fuzzy_rules = dict()
        self.fuzzy_rules["cell"] = {
            "not_data": {
                "First_FW_Symbol_disagrees": {"name": "", "theme": "SYMBOLS"},
                "First_BW_Symbol_disagrees": {"name": "", "theme": "SYMBOLS"},
                "SymbolChain": {"name": "", "theme": "SYMBOLS"},
                "CC": {"name": "", "theme": "CASE"},
                "CONSISTENT_NUMERIC": {
                    "name": "Below but not here: consistently ONE symbol = D",
                    "theme": "SYMBOLS",
                },
                "CONSISTENT_D_STAR": {
                    "name": "Below but not here: consistently TWO symbols, the first is a digit",
                    "theme": "SYMBOLS",
                },
                "FW_SUMMARY_D": {
                    "name": "Below but not here: two or above symbols in the FW summary, the first is a digit",
                    "theme": "SYMBOLS",
                },
                "BW_SUMMARY_D": {
                    "name": "Below but not here: two or above symbols in the BW summary, the first is a digit",
                    "theme": "SYMBOLS",
                },
                "BROAD_NUMERIC": {
                    "name": "Below but not here: all values digits, optionally have . or ,  or S",
                    "theme": "SYMBOLS",
                },
                "FW_THREE_OR_MORE_NO_SPACE": {
                    "name": "Below but not here: three or above symbols in FW summary that do not contain a  Space",
                    "theme": "SYMBOLS",
                },
                "BW_THREE_OR_MORE_NO_SPACE": {
                    "name": "Below but not here: three or above symbols in BW summary that do not contain a  Space",
                    "theme": "SYMBOLS",
                },
                "CONSISTENT_SS_NO_SPACE": {
                    "name": "Below but not here: consistently at least two symbols in the symbol set summary, none of which are S or _",
                    "theme": "SYMBOLS",
                },
                "FW_TWO_OR_MORE_OR_MORE_NO_SPACE": {
                    "name": "Below but not here: two or above symbols in FW summary that do not contain a Space",
                    "theme": "SYMBOLS",
                },
                "BW_TWO_OR_MORE_OR_MORE_NO_SPACE": {
                    "name": "Below but not here: two or above symbols in BW summary that do not contain a Space",
                    "theme": "SYMBOLS",
                },
                "CHAR_COUNT_UNDER_POINT1_MIN": {"name": "", "theme": "LENGTH_CTXT"},
                "CHAR_COUNT_UNDER_POINT3_MIN": {"name": "", "theme": "LENGTH_CTXT"},
                # "CHAR_COUNT_UNDER_POINT5_MIN":{
                #     "name":"",
                #     "theme":"LENGTH_CTXT"
                #     },
                "CHAR_COUNT_OVER_POINT5_MAX": {"name": "", "theme": "LENGTH_CTXT"},
                "CHAR_COUNT_OVER_POINT6_MAX": {"name": "", "theme": "LENGTH_CTXT"},
                "CHAR_COUNT_OVER_POINT7_MAX": {"name": "", "theme": "LENGTH_CTXT"},
                "CHAR_COUNT_OVER_POINT8_MAX": {"name": "", "theme": "LENGTH_CTXT"},
                "CHAR_COUNT_OVER_POINT9_MAX": {"name": "", "theme": "LENGTH_CTXT"},
                "NON_NUMERIC_CHAR_COUNT_DIFFERS_FROM_CONSISTENT": {
                    "name": "",
                    "theme": "LENGTH_CTXT",
                }
                # "CHAR_COUNT_DIFFERS_FROM_CONSISTENT":{"name":""}
            },
            "data": {
                "VALUE_REPEATS_ONCE_BELOW": {
                    "name": "Rule_1_a value repetition only once in values below me, skip the adjacent value",
                    "theme": "VALUE_CTXT",
                },
                "VALUE_REPEATS_TWICE_OR_MORE_BELOW": {
                    "name": "Rule_1_b value repetition twice or more below me",
                    "theme": "VALUE_CTXT",
                },
                "ONE_ALPHA_TOKEN_REPEATS_ONCE_BELOW": {
                    "name": "Rule_2_a: Only one alphabetic token from multiword value repeats below, and it repeats only once",
                    "theme": "VALUE_CTXT",
                },
                "ALPHA_TOKEN_REPEATS_TWICE_OR_MORE": {
                    "name": "Rule_2_b: At least one alphabetic token from multiword value repeats below at least twice",
                    "theme": "VALUE_CTXT",
                },
                "CONSISTENT_NUMERIC_WIDTH": {
                    "name": "Rule_3 consistently numbers with consistent digit count for all.",
                    "theme": "SYMBOLS",
                },
                "CONSISTENT_NUMERIC": {
                    "name": "Rule_4_a consistently ONE symbol = D",
                    "theme": "SYMBOLS",
                },
                "CONSISTENT_D_STAR": {
                    "name": "Rule_4_b consistently TWO symbols, the first is a digit",
                    "theme": "SYMBOLS",
                },
                "FW_SUMMARY_D": {
                    "name": "Rule_4_fw two or above symbols in the FW summary, the first is a digit",
                    "theme": "SYMBOLS",
                },
                "BW_SUMMARY_D": {
                    "name": "Rule_4_bw two or above symbols in the BW summary, the first is a digit",
                    "theme": "SYMBOLS",
                },
                "BROAD_NUMERIC": {
                    "name": "Rule_5 all values digits, optionally have . or ,  or S",
                    "theme": "SYMBOLS",
                },
                "FW_THREE_OR_MORE_NO_SPACE": {
                    "name": "Rule_6 three or above symbols in FW summary that do not contain a  Space",
                    "theme": "SYMBOLS",
                },
                "BW_THREE_OR_MORE_NO_SPACE": {
                    "name": "Rule_7 three or above symbols in BW summary that do not contain a  Space",
                    "theme": "SYMBOLS",
                },
                "CONSISTENT_SS_NO_SPACE": {
                    "name": "Rule_8 consistently at least two symbols in the symbol set summary, none of which are S or _",
                    "theme": "SYMBOLS",
                },
                "CONSISTENT_SC_TWO_OR_MORE": {
                    "name": "Rule_10 two or more symbols consistent chain",
                    "theme": "SYMBOLS",
                },
                "FW_TWO_OR_MORE_OR_MORE_NO_SPACE": {
                    "name": "Rule_11_fw two or above symbols in FW summary that do not contain a Space",
                    "theme": "SYMBOLS",
                },
                "BW_TWO_OR_MORE_OR_MORE_NO_SPACE": {
                    "name": "Rule_11_bw two or above symbols in BW summary that do not contain a Space",
                    "theme": "SYMBOLS",
                },
                "FW_TWO_OR_MORE_OR_MORE_NO_SPACE_FIRST_TWO": {
                    "name": "Rule_12_fw two or above symbols in FW summary, the first two do not contain a Space",
                    "theme": "SYMBOLS",
                },
                "BW_TWO_OR_MORE_OR_MORE_NO_SPACE_FIRST_TWO": {
                    "name": "Rule_12_bw two or above symbols in BW summary, the first two do not contain a Space",
                    "theme": "SYMBOLS",
                },
                "FW_D5PLUS": {
                    "name": "Rule_13_fw FW summary is [['D',count]], where count>=5",
                    "theme": "SYMBOLS",
                },
                "BW_D5PLUS": {
                    "name": "Rule_13_bw BW summary is [['D',count]], where count>=5",
                    "theme": "SYMBOLS",
                },
                "FW_D1": {
                    "name": "Rule_14_fw FW summary is [['D',1]]",
                    "theme": "SYMBOLS",
                },
                "BW_D1": {
                    "name": "Rule_14_bw BW summary is [['D',1]]",
                    "theme": "SYMBOLS",
                },
                "FW_D4": {
                    "name": "Rule_15_fw FW summary is [['D',4]]",
                    "theme": "SYMBOLS",
                },
                "BW_D4": {
                    "name": "Rule_15_bw BW summary is [['D',4]]",
                    "theme": "SYMBOLS",
                },
                "FW_LENGTH_4PLUS": {
                    "name": "Rule_17_fw four or more symbols in the FW summary",
                    "theme": "SYMBOLS",
                },
                "BW_LENGTH_4PLUS": {
                    "name": "Rule_17_bw four or more symbols in the BW summary",
                    "theme": "SYMBOLS",
                },
                "CASE_SUMMARY_CAPS": {
                    "name": "Rule_18 case summary is ALL_CAPS",
                    "theme": "CASE",
                },
                "CONSISTENT_CHAR_LENGTH": {
                    "name": "this value and neighboring values are constant char length",
                    "theme": "LENGTH_CTXT",
                },
                "CONSISTENT_SINGLE_WORD_CONSISTENT_CASE": {"name": "", "theme": "CASE"},
            },
        }

        self.fuzzy_rules["line"] = {
            "not_data": {
                "ADJACENT_ARITHMETIC_SEQUENCE_2": {
                    "type": "header",
                    "name": "",
                    "theme": "VALUES_CTXT",
                },
                "ADJACENT_ARITHMETIC_SEQUENCE_3": {
                    "type": "header",
                    "name": "",
                    "theme": "VALUES_CTXT",
                },
                "ADJACENT_ARITHMETIC_SEQUENCE_4": {
                    "type": "header",
                    "name": "",
                    "theme": "VALUES_CTXT",
                },
                "ADJACENT_ARITHMETIC_SEQUENCE_5": {
                    "type": "header",
                    "name": "",
                    "theme": "VALUES_CTXT",
                },
                "ADJACENT_ARITHMETIC_SEQUENCE_6_plus": {
                    "type": "header",
                    "name": "",
                    "theme": "VALUES_CTXT",
                },
                "RANGE_PAIRS_1": {"type": "header", "name": "", "theme": "VALUES_CTXT"},
                "RANGE_PAIRS_2_plus": {
                    "type": "header",
                    "name": "",
                    "theme": "VALUES_CTXT",
                },
                "PARTIALLY_REPEATING_VALUES_length_2_plus": {
                    "type": "header",
                    "name": "",
                    "theme": "VALUES_CTXT",
                },
                "METADATA_LIKE_ROW": {"type": "header", "name": "", "theme": "SYMBOLS"},
                "CONSISTENTLY_SLUG_OR_SNAKE": {
                    "type": "header",
                    "name": "",
                    "theme": "CASE",
                },
                "CONSISTENTLY_UPPER_CASE": {"type": "header", "theme": "CASE"},
                "AGGREGATION_ON_ROW_WO_NUMERIC": {
                    "type": "aggregation",
                    "name": "",
                    "theme": "VALUES_CTXT",
                },
                "AGGREGATION_ON_ROW_W_ARITH_SEQUENCE": {
                    "type": "aggregation",
                    "name": "",
                    "theme": "VALUES_CTXT",
                },
                # ,"MULTIPLE_AGGREGATION_VALUES_ON_ROW":{
                # "type":"aggregation",
                # "name":""
                # },
                "UP_TO_FIRST_COLUMN_COMPLETE_CONSISTENTLY": {
                    "type": "other",
                    "name": "all lines consistently non-data from beginning of input, left-most column potentially non-null",
                    "theme": "SYMBOLS",
                },
                "STARTS_WITH_NULL": {
                    "type": "other",
                    "name": "line starts with null",
                    "theme": "SYMBOLS",
                },
                "NO_SUMMARY_BELOW": {
                    "type": "other",
                    "name": "no summary achieved below in any column",
                    "theme": "SYMBOLS",
                },
                "FOOTNOTE": {
                    "type": "other",
                    "name": "line resembles footnote",
                    "theme": "VALUES",
                },
                "METADATA_TABLE_HEADER_KEYWORDS": {
                    "type": "header",
                    "name": "Line has cells with metadata specifiers",
                    "theme": "VALUES",
                },
            },
            "data": {
                "AGGREGATION_TOKEN_IN_FIRST_VALUE_OF_ROW": {
                    "type": "aggregation",
                    "name": "First value of a row (first column) contains an aggregation token, this is likely a summarizing data line",
                    "theme": "VALUES_CTXT",
                },
                "NULL_EQUIVALENT_ON_LINE_2_PLUS": {
                    "type": "other",
                    "name": "Two or more null equivalent values found on a line",
                    "theme": "VALUES",
                },
                "ONE_NULL_EQUIVALENT_ON_LINE": {
                    "type": "other",
                    "name": "One null equivalent value found on line",
                    "theme": "VALUES",
                },
                "CONTAINS_DATATYPE_CELL_VALUE": {
                    "type": "other",
                    "name": "Line has cell value taken from datatype keyword list",
                    "theme": "VALUES",
                },
            },
        }

    def leave_rules_out(self, ignore_rules):
        self.ignore_rules = ignore_rules

    def save_weights(self, filepath="trained_rules.json"):
        with open(filepath, "w") as outfile:
            json.dump(self.fuzzy_rules, outfile)

    def load_model(self, fuzzy_rules):
        self.fuzzy_rules = fuzzy_rules

    def load_weights(self, filepath):
        with open(filepath) as json_file:
            self.fuzzy_rules = json.load(json_file)

    def load_default_weights(self):
        self.fuzzy_rules = {
            "cell": {
                "not_data": {
                    "First_FW_Symbol_disagrees": {
                        "name": "",
                        "theme": "SYMBOLS",
                        "weight": 0.9920420998588115,
                        "confidence": 0.9960210499294058,
                        "coverage": 0.12201844920204852,
                    },
                    "First_BW_Symbol_disagrees": {
                        "name": "",
                        "theme": "SYMBOLS",
                        "weight": 0.9853814527181362,
                        "confidence": 0.9926907263590681,
                        "coverage": 0.13713175987846707,
                    },
                    "SymbolChain": {
                        "name": "",
                        "theme": "SYMBOLS",
                        "weight": 0.9845763019415714,
                        "confidence": 0.9922881509707857,
                        "coverage": 0.17262063240982914,
                    },
                    "CC": {
                        "name": "",
                        "theme": "CASE",
                        "weight": 0.9865549493374903,
                        "confidence": 0.9932774746687452,
                        "coverage": 0.1607492443344662,
                    },
                    "CONSISTENT_NUMERIC": {
                        "name": "Below but not here: consistently ONE symbol = D",
                        "theme": "SYMBOLS",
                        "weight": 0.996462524872872,
                        "confidence": 0.998231262436436,
                        "coverage": 0.07083679190615652,
                    },
                    "CONSISTENT_D_STAR": {
                        "name": "Below but not here: consistently TWO symbols, the first is a digit",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 3.132292368169645e-05,
                    },
                    "FW_SUMMARY_D": {
                        "name": "Below but not here: two or above symbols in the FW summary, the first is a digit",
                        "theme": "SYMBOLS",
                        "weight": 0.9887755102040816,
                        "confidence": 0.9943877551020408,
                        "coverage": 0.03069646520806252,
                    },
                    "BW_SUMMARY_D": {
                        "name": "Below but not here: two or above symbols in the BW summary, the first is a digit",
                        "theme": "SYMBOLS",
                        "weight": 0.9857651245551602,
                        "confidence": 0.9928825622775801,
                        "coverage": 0.04400870777278351,
                    },
                    "BROAD_NUMERIC": {
                        "name": "Below but not here: all values digits, optionally have . or ,  or S",
                        "theme": "SYMBOLS",
                        "weight": 0.9961234531086924,
                        "confidence": 0.9980617265543462,
                        "coverage": 0.10504142456656905,
                    },
                    "FW_THREE_OR_MORE_NO_SPACE": {
                        "name": "Below but not here: three or above symbols in FW summary that do not contain a  Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9783571077225774,
                        "confidence": 0.9891785538612887,
                        "coverage": 0.03183975192244444,
                    },
                    "BW_THREE_OR_MORE_NO_SPACE": {
                        "name": "Below but not here: three or above symbols in BW summary that do not contain a  Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9785575048732944,
                        "confidence": 0.9892787524366472,
                        "coverage": 0.032137319697420556,
                    },
                    "CONSISTENT_SS_NO_SPACE": {
                        "name": "Below but not here: consistently at least two symbols in the symbol set summary, none of which are S or _",
                        "theme": "SYMBOLS",
                        "weight": 0.9836065573770492,
                        "confidence": 0.9918032786885246,
                        "coverage": 0.04394606192542012,
                    },
                    "FW_TWO_OR_MORE_OR_MORE_NO_SPACE": {
                        "name": "Below but not here: two or above symbols in FW summary that do not contain a Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9823757490306662,
                        "confidence": 0.9911878745153331,
                        "coverage": 0.04443156724248641,
                    },
                    "BW_TWO_OR_MORE_OR_MORE_NO_SPACE": {
                        "name": "Below but not here: two or above symbols in BW summary that do not contain a Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9797415298637793,
                        "confidence": 0.9898707649318896,
                        "coverage": 0.04483876525034847,
                    },
                    "CHAR_COUNT_UNDER_POINT1_MIN": {
                        "name": "",
                        "theme": "LENGTH_CTXT",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0002505833894535716,
                    },
                    "CHAR_COUNT_UNDER_POINT3_MIN": {
                        "name": "",
                        "theme": "LENGTH_CTXT",
                        "weight": 0.8419753086419752,
                        "confidence": 0.9209876543209876,
                        "coverage": 0.006342892045543531,
                    },
                    "CHAR_COUNT_OVER_POINT5_MAX": {
                        "name": "",
                        "theme": "LENGTH_CTXT",
                        "weight": 0.9847685998828355,
                        "confidence": 0.9923842999414177,
                        "coverage": 0.1336705768116396,
                    },
                    "CHAR_COUNT_OVER_POINT6_MAX": {
                        "name": "",
                        "theme": "LENGTH_CTXT",
                        "weight": 0.9873217115689381,
                        "confidence": 0.993660855784469,
                        "coverage": 0.12847097148047798,
                    },
                    "CHAR_COUNT_OVER_POINT7_MAX": {
                        "name": "",
                        "theme": "LENGTH_CTXT",
                        "weight": 0.9886277482941623,
                        "confidence": 0.9943138741470812,
                        "coverage": 0.12394480900847285,
                    },
                    "CHAR_COUNT_OVER_POINT8_MAX": {
                        "name": "",
                        "theme": "LENGTH_CTXT",
                        "weight": 0.9900370417677864,
                        "confidence": 0.9950185208838932,
                        "coverage": 0.12261358475200075,
                    },
                    "CHAR_COUNT_OVER_POINT9_MAX": {
                        "name": "",
                        "theme": "LENGTH_CTXT",
                        "weight": 0.9910490983282876,
                        "confidence": 0.9955245491641438,
                        "coverage": 0.11898012560492396,
                    },
                    "NON_NUMERIC_CHAR_COUNT_DIFFERS_FROM_CONSISTENT": {
                        "name": "",
                        "theme": "LENGTH_CTXT",
                        "weight": 0.9442748091603053,
                        "confidence": 0.9721374045801526,
                        "coverage": 0.041033030023022346,
                    },
                },
                "data": {
                    "VALUE_REPEATS_ONCE_BELOW": {
                        "name": "Rule_1_a value repetition only once in values below me, skip the adjacent value",
                        "theme": "VALUE_CTXT",
                        "weight": 0.9988256018790369,
                        "confidence": 0.9994128009395185,
                        "coverage": 0.026671469514964526,
                    },
                    "VALUE_REPEATS_TWICE_OR_MORE_BELOW": {
                        "name": "Rule_1_b value repetition twice or more below me",
                        "theme": "VALUE_CTXT",
                        "weight": 0.9995367689635206,
                        "confidence": 0.9997683844817603,
                        "coverage": 0.13523672299572442,
                    },
                    "ONE_ALPHA_TOKEN_REPEATS_ONCE_BELOW": {
                        "name": "Rule_2_a: Only one alphabetic token from multiword value repeats below, and it repeats only once",
                        "theme": "VALUE_CTXT",
                        "weight": -0.6447283162084607,
                        "confidence": 0.1776358418957697,
                        "coverage": 0.16919077226668336,
                    },
                    "ALPHA_TOKEN_REPEATS_TWICE_OR_MORE": {
                        "name": "Rule_2_b: At least one alphabetic token from multiword value repeats below at least twice",
                        "theme": "VALUE_CTXT",
                        "weight": 0.17641820286757498,
                        "confidence": 0.5882091014337875,
                        "coverage": 0.1758625550108847,
                    },
                    "CONSISTENT_NUMERIC_WIDTH": {
                        "name": "Rule_3 consistently numbers with consistent digit count for all.",
                        "theme": "SYMBOLS",
                        "weight": 0.9980462390100944,
                        "confidence": 0.9990231195050472,
                        "coverage": 0.0961926986264898,
                    },
                    "CONSISTENT_NUMERIC": {
                        "name": "Rule_4_a consistently ONE symbol = D",
                        "theme": "SYMBOLS",
                        "weight": 0.966147719044171,
                        "confidence": 0.9830738595220855,
                        "coverage": 0.17302783041769118,
                    },
                    "CONSISTENT_D_STAR": {
                        "name": "Rule_4_b consistently TWO symbols, the first is a digit",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 4.698438552254468e-05,
                    },
                    "FW_SUMMARY_D": {
                        "name": "Rule_4_fw two or above symbols in the FW summary, the first is a digit",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.07500274075582215,
                    },
                    "BW_SUMMARY_D": {
                        "name": "Rule_4_bw two or above symbols in the BW summary, the first is a digit",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.10158024149974158,
                    },
                    "BROAD_NUMERIC": {
                        "name": "Rule_5 all values digits, optionally have . or ,  or S",
                        "theme": "SYMBOLS",
                        "weight": 0.954963069717168,
                        "confidence": 0.977481534858584,
                        "coverage": 0.26081032403564547,
                    },
                    "FW_THREE_OR_MORE_NO_SPACE": {
                        "name": "Rule_6 three or above symbols in FW summary that do not contain a  Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9983572895277208,
                        "confidence": 0.9991786447638604,
                        "coverage": 0.07627131916493085,
                    },
                    "BW_THREE_OR_MORE_NO_SPACE": {
                        "name": "Rule_7 three or above symbols in BW summary that do not contain a  Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9991915925626516,
                        "confidence": 0.9995957962813258,
                        "coverage": 0.07749291318851702,
                    },
                    "CONSISTENT_SS_NO_SPACE": {
                        "name": "Rule_8 consistently at least two symbols in the symbol set summary, none of which are S or _",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.10049960063272306,
                    },
                    "CONSISTENT_SC_TWO_OR_MORE": {
                        "name": "Rule_10 two or more symbols consistent chain",
                        "theme": "SYMBOLS",
                        "weight": 0.9855635757912271,
                        "confidence": 0.9927817878956136,
                        "coverage": 0.11282517110147061,
                    },
                    "FW_TWO_OR_MORE_OR_MORE_NO_SPACE": {
                        "name": "Rule_11_fw two or above symbols in FW summary that do not contain a Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9987650509416486,
                        "confidence": 0.9993825254708243,
                        "coverage": 0.1014549498050148,
                    },
                    "BW_TWO_OR_MORE_OR_MORE_NO_SPACE": {
                        "name": "Rule_11_bw two or above symbols in BW summary that do not contain a Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9990870359099209,
                        "confidence": 0.9995435179549604,
                        "coverage": 0.10292712721805454,
                    },
                    "FW_TWO_OR_MORE_OR_MORE_NO_SPACE_FIRST_TWO": {
                        "name": "Rule_12_fw two or above symbols in FW summary, the first two do not contain a Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9985136741973841,
                        "confidence": 0.9992568370986921,
                        "coverage": 0.10537031526522686,
                    },
                    "BW_TWO_OR_MORE_OR_MORE_NO_SPACE_FIRST_TWO": {
                        "name": "Rule_12_bw two or above symbols in BW summary, the first two do not contain a Space",
                        "theme": "SYMBOLS",
                        "weight": 0.9985367281240856,
                        "confidence": 0.9992683640620428,
                        "coverage": 0.10703043022035677,
                    },
                    "FW_D5PLUS": {
                        "name": "Rule_13_fw FW summary is [['D',count]], where count>=5",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.007313902679676121,
                    },
                    "BW_D5PLUS": {
                        "name": "Rule_13_bw BW summary is [['D',count]], where count>=5",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.007282579755994424,
                    },
                    "FW_D1": {
                        "name": "Rule_14_fw FW summary is [['D',1]]",
                        "theme": "SYMBOLS",
                        "weight": 0.9873786407766991,
                        "confidence": 0.9936893203883496,
                        "coverage": 0.032262611392147346,
                    },
                    "BW_D1": {
                        "name": "Rule_14_bw BW summary is [['D',1]]",
                        "theme": "SYMBOLS",
                        "weight": 0.9835986493005306,
                        "confidence": 0.9917993246502653,
                        "coverage": 0.03246621039607837,
                    },
                    "FW_D4": {
                        "name": "Rule_15_fw FW summary is [['D',4]]",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.012873721633177241,
                    },
                    "BW_D4": {
                        "name": "Rule_15_bw BW summary is [['D',4]]",
                        "theme": "SYMBOLS",
                        "weight": 0.9853658536585366,
                        "confidence": 0.9926829268292683,
                        "coverage": 0.012842398709495544,
                    },
                    "FW_LENGTH_4PLUS": {
                        "name": "Rule_17_fw four or more symbols in the FW summary",
                        "theme": "SYMBOLS",
                        "weight": 0.8693244739756367,
                        "confidence": 0.9346622369878184,
                        "coverage": 0.04242690012685784,
                    },
                    "BW_LENGTH_4PLUS": {
                        "name": "Rule_17_bw four or more symbols in the BW summary",
                        "theme": "SYMBOLS",
                        "weight": 0.8856485034535686,
                        "confidence": 0.9428242517267843,
                        "coverage": 0.040813769557250475,
                    },
                    "CASE_SUMMARY_CAPS": {
                        "name": "Rule_18 case summary is ALL_CAPS",
                        "theme": "CASE",
                        "weight": 0.4848311390955924,
                        "confidence": 0.7424155695477962,
                        "coverage": 0.027360573835961847,
                    },
                    "CONSISTENT_CHAR_LENGTH": {
                        "name": "this value and neighboring values are constant char length",
                        "theme": "LENGTH_CTXT",
                        "weight": 0.9043583535108959,
                        "confidence": 0.9521791767554479,
                        "coverage": 0.36221828945513773,
                    },
                    "CONSISTENT_SINGLE_WORD_CONSISTENT_CASE": {
                        "name": "",
                        "theme": "CASE",
                        "weight": 0.8904306220095695,
                        "confidence": 0.9452153110047847,
                        "coverage": 0.06546491049474558,
                    },
                },
            },
            "line": {
                "not_data": {
                    "ADJACENT_ARITHMETIC_SEQUENCE_2": {
                        "type": "header",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": None,
                        "confidence": None,
                        "coverage": 0.0,
                    },
                    "ADJACENT_ARITHMETIC_SEQUENCE_3": {
                        "type": "header",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0001431229426077,
                    },
                    "ADJACENT_ARITHMETIC_SEQUENCE_4": {
                        "type": "header",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0037211965078002003,
                    },
                    "ADJACENT_ARITHMETIC_SEQUENCE_5": {
                        "type": "header",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0005724917704308,
                    },
                    "ADJACENT_ARITHMETIC_SEQUENCE_6_plus": {
                        "type": "header",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0038643194504079004,
                    },
                    "RANGE_PAIRS_1": {
                        "type": "header",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": 0.5714285714285714,
                        "confidence": 0.7857142857142857,
                        "coverage": 0.0040074423930156,
                    },
                    "RANGE_PAIRS_2_plus": {
                        "type": "header",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": 0.8904109589041096,
                        "confidence": 0.9452054794520548,
                        "coverage": 0.0104479748103621,
                    },
                    "PARTIALLY_REPEATING_VALUES_length_2_plus": {
                        "type": "header",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": 0.8095238095238095,
                        "confidence": 0.9047619047619048,
                        "coverage": 0.0030055817947617003,
                    },
                    "METADATA_LIKE_ROW": {
                        "type": "header",
                        "name": "",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0140260483755546,
                    },
                    "CONSISTENTLY_SLUG_OR_SNAKE": {
                        "type": "header",
                        "name": "",
                        "theme": "CASE",
                        "weight": 0.9893617021276595,
                        "confidence": 0.9946808510638298,
                        "coverage": 0.026907113210247604,
                    },
                    "CONSISTENTLY_UPPER_CASE": {
                        "type": "header",
                        "theme": "CASE",
                        "weight": 0.9710144927536233,
                        "confidence": 0.9855072463768116,
                        "coverage": 0.0395019321597252,
                    },
                    "AGGREGATION_ON_ROW_WO_NUMERIC": {
                        "type": "aggregation",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.044081866323171605,
                    },
                    "AGGREGATION_ON_ROW_W_ARITH_SEQUENCE": {
                        "type": "aggregation",
                        "name": "",
                        "theme": "VALUES_CTXT",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0008587376556462001,
                    },
                    "UP_TO_FIRST_COLUMN_COMPLETE_CONSISTENTLY": {
                        "type": "other",
                        "name": "all lines consistently non-data from beginning of input, left-most column potentially non-null",
                        "theme": "SYMBOLS",
                        "weight": 0.9956989247311827,
                        "confidence": 0.9978494623655914,
                        "coverage": 0.13310433662516102,
                    },
                    "STARTS_WITH_NULL": {
                        "type": "other",
                        "name": "line starts with null",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0655503077143266,
                    },
                    "NO_SUMMARY_BELOW": {
                        "type": "other",
                        "name": "no summary achieved below in any column",
                        "theme": "SYMBOLS",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0020037211965078,
                    },
                    "FOOTNOTE": {
                        "type": "other",
                        "name": "line resembles footnote",
                        "theme": "VALUES",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0011449835408616,
                    },
                    "METADATA_TABLE_HEADER_KEYWORDS": {
                        "type": "header",
                        "name": "Line has cells with metadata specifiers",
                        "theme": "VALUES",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.005152425933877201,
                    },
                },
                "data": {
                    "AGGREGATION_TOKEN_IN_FIRST_VALUE_OF_ROW": {
                        "type": "aggregation",
                        "name": "First value of a row (first column) contains an aggregation token, this is likely a summarizing data line",
                        "theme": "VALUES_CTXT",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0032918276799771004,
                    },
                    "NULL_EQUIVALENT_ON_LINE_2_PLUS": {
                        "type": "other",
                        "name": "Two or more null equivalent values found on a line",
                        "theme": "VALUES",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.15571776155717762,
                    },
                    "ONE_NULL_EQUIVALENT_ON_LINE": {
                        "type": "other",
                        "name": "One null equivalent value found on line",
                        "theme": "VALUES",
                        "weight": 0.9961051606621227,
                        "confidence": 0.9980525803310614,
                        "coverage": 0.14698726205810791,
                    },
                    "CONTAINS_DATATYPE_CELL_VALUE": {
                        "type": "other",
                        "name": "Line has cell value taken from datatype keyword list",
                        "theme": "VALUES",
                        "weight": 1.0,
                        "confidence": 1.0,
                        "coverage": 0.0002862458852154,
                    },
                },
            },
        }

    def train_rules(
        self,
        undersampled_cell_data,
        undersampled_cell_not_data,
        undersampled_line_data,
        undersampled_line_not_data,
    ):

        # ##### DROP empty lines ########################################################################
        # # Discover cells that belong to empty lines
        # blankline_cells = undersampled_cell_data.groupby(['crawl_datafile_key', 'line_index']).filter(lambda group: (group.label=='BLANK').all())
        # nonblankline_cells = undersampled_cell_data.groupby(['crawl_datafile_key', 'line_index']).filter(lambda group: (group.label!='BLANK').any())

        # # Keep only cells from non-empty lines
        # undersampled_cell_data=undersampled_cell_data.loc[nonblankline_cells.index]
        # undersampled_cell_not_data=undersampled_cell_not_data.loc[nonblankline_cells.index]

        # empty_lines=blankline_cells[['crawl_datafile_key', 'line_index']].drop_duplicates()
        # undersampled_line_data=pd.merge(undersampled_line_data,
        #                                 empty_lines,
        #                                 on=['crawl_datafile_key','line_index'],
        #                                 how="outer", indicator=True
        #         ).query('_merge=="left_only"').drop('_merge', axis=1)
        # undersampled_line_not_data=pd.merge(undersampled_line_not_data,
        #                                     empty_lines,
        #                                     on=['crawl_datafile_key','line_index'],
        #                                     how="outer", indicator=True
        #         ).query('_merge=="left_only"').drop('_merge', axis=1)
        # #-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#-#

        total_cell_instances = undersampled_cell_data.shape[0]
        data_cell_instances = undersampled_cell_data.query("label=='DATA'").shape[0]
        not_data_cell_instances = undersampled_cell_data.query("label!='DATA'").shape[0]

        total_line_instances = undersampled_line_data.shape[0]
        data_line_instances = undersampled_line_data.query("label=='DATA'").shape[0]
        not_data_line_instances = undersampled_line_data.query("label!='DATA'").shape[0]

        for rule in self.fuzzy_rules["cell"]["data"].keys():
            data_cell_predicted_positive = 0
            data_cell_true_positive = 0
            data_cell_false_positive = 0

            if rule.lower() in undersampled_cell_data.columns:
                data_cell_predicted_positive = undersampled_cell_data.query(
                    f"{rule.lower()}==True"
                ).shape[0]
                data_cell_true_positive = undersampled_cell_data.query(
                    f"{rule.lower()}==True and label=='DATA'"
                ).shape[0]
                data_cell_false_positive = undersampled_cell_data.query(
                    f"{rule.lower()}==True and label!='DATA'"
                ).shape[0]

            rule_weight = None
            confidence = None

            if data_cell_predicted_positive != 0:
                rule_weight = data_cell_true_positive / data_cell_predicted_positive - (
                    data_cell_false_positive / data_cell_predicted_positive
                )
                confidence = data_cell_true_positive / data_cell_predicted_positive

            coverage = data_cell_predicted_positive / total_cell_instances

            self.fuzzy_rules["cell"]["data"][rule]["weight"] = rule_weight
            self.fuzzy_rules["cell"]["data"][rule]["confidence"] = confidence
            self.fuzzy_rules["cell"]["data"][rule]["coverage"] = coverage

        for rule in self.fuzzy_rules["cell"]["not_data"].keys():
            not_data_cell_predicted_positive = 0
            not_data_cell_true_positive = 0
            not_data_cell_false_positive = 0

            if rule.lower() in undersampled_cell_not_data.columns:
                not_data_cell_predicted_positive = undersampled_cell_not_data.query(
                    f"{rule.lower()}==True"
                ).shape[0]
                not_data_cell_true_positive = undersampled_cell_not_data.query(
                    f"{rule.lower()}==True and label!='DATA'"
                ).shape[0]
                not_data_cell_false_positive = undersampled_cell_not_data.query(
                    f"{rule.lower()}==True and label=='DATA'"
                ).shape[0]

            rule_weight = None
            confidence = None

            if not_data_cell_predicted_positive != 0:
                rule_weight = (
                    not_data_cell_true_positive / not_data_cell_predicted_positive
                    - (not_data_cell_false_positive / not_data_cell_predicted_positive)
                )
                confidence = (
                    not_data_cell_true_positive / not_data_cell_predicted_positive
                )

            coverage = not_data_cell_predicted_positive / total_cell_instances

            self.fuzzy_rules["cell"]["not_data"][rule]["weight"] = rule_weight
            self.fuzzy_rules["cell"]["not_data"][rule]["confidence"] = confidence
            self.fuzzy_rules["cell"]["not_data"][rule]["coverage"] = coverage

        for rule in self.fuzzy_rules["line"]["data"].keys():
            data_line_predicted_positive = 0
            data_line_true_positive = 0
            data_line_false_positive = 0
            if rule.lower() in undersampled_line_data.columns:
                data_line_predicted_positive = undersampled_line_data.query(
                    f"{rule.lower()}==True"
                ).shape[0]
                data_line_true_positive = undersampled_line_data.query(
                    f"{rule.lower()}==True and label=='DATA'"
                ).shape[0]
                data_line_false_positive = undersampled_line_data.query(
                    f"{rule.lower()}==True and label!='DATA'"
                ).shape[0]

            rule_weight = None
            confidence = None

            if data_line_predicted_positive != 0:
                rule_weight = data_line_true_positive / data_line_predicted_positive - (
                    data_line_false_positive / data_line_predicted_positive
                )
                confidence = data_line_true_positive / data_line_predicted_positive

            coverage = data_line_predicted_positive / total_line_instances

            self.fuzzy_rules["line"]["data"][rule]["weight"] = rule_weight
            self.fuzzy_rules["line"]["data"][rule]["confidence"] = confidence
            self.fuzzy_rules["line"]["data"][rule]["coverage"] = coverage

        for rule in self.fuzzy_rules["line"]["not_data"].keys():
            not_data_line_predicted_positive = 0
            not_data_line_true_positive = 0
            not_data_line_false_positive = 0

            if rule.lower() in undersampled_line_not_data.columns:
                not_data_line_predicted_positive = undersampled_line_not_data.query(
                    f"{rule.lower()}==True"
                ).shape[0]
                not_data_line_true_positive = undersampled_line_not_data.query(
                    f"{rule.lower()}==True and label!='DATA'"
                ).shape[0]
                not_data_line_false_positive = undersampled_line_not_data.query(
                    f"{rule.lower()}==True and label=='DATA'"
                ).shape[0]

            rule_weight = None
            confidence = None

            if not_data_line_predicted_positive != 0:
                rule_weight = (
                    not_data_line_true_positive / not_data_line_predicted_positive
                    - (not_data_line_false_positive / not_data_line_predicted_positive)
                )
                confidence = (
                    not_data_line_true_positive / not_data_line_predicted_positive
                )

            coverage = not_data_line_predicted_positive / total_line_instances

            self.fuzzy_rules["line"]["not_data"][rule]["weight"] = rule_weight
            self.fuzzy_rules["line"]["not_data"][rule]["confidence"] = confidence
            self.fuzzy_rules["line"]["not_data"][rule]["coverage"] = coverage

    def extract_tables(self, file_dataframe_trimmed, blank_lines, rules_fired=None):
        # initialize
        discovered_tables = SortedDict()
        # try:
        signatures = TableSignatures(
            file_dataframe_trimmed, self.parameters.outlier_sensitive
        )

        if rules_fired is None:
            data_rules_fired, not_data_rules_fired = collect_dataframe_rules(
                file_dataframe_trimmed, self, signatures
            )
        else:
            data_rules_fired, not_data_rules_fired = rules_fired

        table_counter = 1
        file_offset = 0
        headers_discovered = dict()
        discovered_table = discover_next_table(
            file_dataframe_trimmed,
            file_offset,
            table_counter,
            data_rules_fired,
            not_data_rules_fired,
            blank_lines,
            headers_discovered,
            signatures,
            self,
        )

        while discovered_table != None:
            discovered_tables[table_counter] = discovered_table
            for h in discovered_table["header"]:
                headers_discovered[h] = ",".join(
                    file_dataframe_trimmed.loc[h].apply(str).tolist()
                )

            table_counter += 1
            file_offset = discovered_table["data_end"] + 1
            # print(f'file_offset={file_offset}')
            discovered_table = discover_next_table(
                file_dataframe_trimmed,
                file_offset,
                table_counter,
                data_rules_fired,
                not_data_rules_fired,
                blank_lines,
                headers_discovered,
                signatures,
                self,
            )

            # print(f'discovered_table={discovered_table}')
            if discovered_table != None and set(
                range(
                    discovered_tables[table_counter - 1]["data_end"] + 1,
                    discovered_table["data_start"],
                )
            ).issubset(
                set(
                    blank_lines
                    + list(
                        set(
                            range(
                                discovered_table["top_boundary"],
                                discovered_table["data_start"],
                            )
                        )
                        - set(discovered_table["header"])
                    )
                )
            ):  # discovered_table["data_start"]==discovered_tables[table_counter-1]["data_end"]+1) :
                # pp.pprint(discovered_tables)
                # input(f'table_counter={table_counter}')
                discovered_table = merge_tables(
                    discovered_tables[table_counter - 1], discovered_table
                )
                file_offset = discovered_table["data_end"] + 1
                # print(f'file_offset={file_offset}')
                table_counter -= 1

            if discovered_table != None:
                if (
                    table_counter - 1 in discovered_tables.keys()
                    and len(discovered_tables[table_counter - 1]["footnotes"]) > 0
                ):
                    discovered_tables[table_counter - 1]["footnotes"] = list(
                        range(
                            discovered_tables[table_counter - 1]["footnotes"][0],
                            discovered_table["top_boundary"],
                        )
                    )
                if table_counter - 1 in discovered_tables.keys():
                    discovered_tables[table_counter - 1]["bottom_boundary"] = (
                        discovered_table["top_boundary"] - 1
                    )
                    discovered_tables[table_counter - 1][
                        "data_end_confidence"
                    ] = discovered_table["data_end_confidence"]

                # print(f'->discovered_table={discovered_table}')
                if discovered_table["data_end"] < file_dataframe_trimmed.shape[0] - 1:
                    discovered_table["footnotes"] = list(
                        range(
                            discovered_table["data_end"] + 1,
                            file_dataframe_trimmed.shape[0],
                        )
                    )
                else:
                    discovered_table["footnotes"] = []
                # print(f'-->discovered_table={discovered_table}')
                if len(discovered_table["footnotes"]) > 0:
                    discovered_table["bottom_boundary"] = discovered_table["footnotes"][
                        -1
                    ]
                else:
                    discovered_table["bottom_boundary"] = discovered_table["data_end"]
                # print(f'--->discovered_table={discovered_table}')
        if (
            table_counter - 1 in discovered_tables.keys()
            and discovered_tables[table_counter - 1]["data_end"]
            != file_dataframe_trimmed.shape[0]
        ):
            footnotes = sorted(
                list(
                    set(
                        range(
                            discovered_tables[table_counter - 1]["data_end"] + 1,
                            file_dataframe_trimmed.shape[0],
                        )
                    )
                    - set(blank_lines)
                )
            )
            discovered_tables[table_counter - 1]["footnotes"] = footnotes

        # except Exception as e:
        #     print(f'crawl_datafile_key={crawl_datafile_key} failed to process, {e}: {traceback.format_exc()}')
        # #     sys.exit()

        return discovered_tables

    def infer_annotations(self, filepath, max_lines=None):
        """
        Returns a dictionary of annotations for the file located at filepath, optionally limited at max_lines
            Parameters:
                filepath (string): absolute path to file
                max_lines (int): [optional] limit of lines to be processed from file
            Returns:
                annotations (dict): Dictionary representation of inferred file annotations
        """
        # print(f'infer_annotations(filepath={filepath}, max_lines={max_lines})')

        discovered_delimiter = None
        discovered_encoding = None
        num_lines_processed = None
        failure = None
        file_dataframe = None
        annotations = None
        from time import time

        starttime = time()
        try:
            print(f"Starting {time()-starttime:.3f}")
            (
                all_csv_tuples,
                discovered_delimiter,
                discovered_encoding,
                encoding_language,
                encoding_confidence,
                failure,
                blanklines,
                google_detected_lang,
            ) = file_utilities.sample_file(filepath, 10)
            num_lines_processed = 0
            all_csv_tuples = []
            if failure == None:
                with codecs.open(filepath, "rU", encoding=discovered_encoding) as f:
                    chunk = f.read()
                    if chunk:
                        for line in csv.reader(
                            chunk.split("\n"),
                            quotechar='"',
                            delimiter=discovered_delimiter,
                            skipinitialspace=True,
                        ):
                            num_lines_processed += 1
                            if len(line) == 0 or sum(len(s.strip()) for s in line) == 0:
                                blanklines.append(num_lines_processed - 1)
                            all_csv_tuples.append(line)
                            # STOP RETRIEVING LINES FROM THE FILE AT MAX LINES
                            if max_lines != None and num_lines_processed == max_lines:
                                break

                    file_dataframe = file_utilities.merged_df(failure, all_csv_tuples)

                    last_line_processed, file_num_columns = file_dataframe.shape

                    blank_lines = []
                    blank_lines = list(
                        file_dataframe[file_dataframe.isnull().all(axis=1)].index
                    )

                    if self.parameters.max_attributes != None:
                        max_attributes = self.parameters.max_attributes
                        if self.parameters.ignore_left != None:
                            max_attributes = (
                                self.parameters.max_attributes
                                + self.parameters.ignore_left
                            )
                        slice_idx = min(max_attributes, file_dataframe.shape[1]) + 1

                    file_max_columns_processed = file_dataframe.iloc[
                        :, :slice_idx
                    ].shape[1]

                    predictions = self.extract_tables(
                        file_dataframe.iloc[:, :slice_idx], blank_lines
                    )

                    annotations = convert_predictions(
                        predictions,
                        blank_lines,
                        last_line_processed,
                        file_num_columns,
                        file_max_columns_processed,
                    )

        except Exception as e:
            print(
                f"filepath={filepath} failed to process, {e}: {traceback.format_exc()}"
            )
        finally:
            return annotations

    def infer_annotations_from_df(self, df):
        """
        Returns a dictionary of annotations for the file located at filepath, optionally limited at max_lines
            Parameters:
                df (DataFrame): A dataframe
            Returns:
                annotations (dict): Dictionary representation of inferred file annotations
        """
        file_dataframe = df
        annotations = None
        try:
            last_line_processed, file_num_columns = file_dataframe.shape

            blank_lines = []
            blank_lines = list(
                file_dataframe[file_dataframe.isnull().all(axis=1)].index
            )

            if self.parameters.max_attributes != None:
                max_attributes = self.parameters.max_attributes
                if self.parameters.ignore_left != None:
                    max_attributes = (
                        self.parameters.max_attributes + self.parameters.ignore_left
                    )
                slice_idx = min(max_attributes, file_dataframe.shape[1]) + 1

            file_max_columns_processed = file_dataframe.iloc[:, :slice_idx].shape[1]

            predictions = self.extract_tables(
                file_dataframe.iloc[:, :slice_idx], blank_lines
            )

            annotations = convert_predictions(
                predictions,
                blank_lines,
                last_line_processed,
                file_num_columns,
                file_max_columns_processed,
            )

        except Exception as e:
            print(
                f"filepath={filepath} failed to process, {e}: {traceback.format_exc()}"
            )
        finally:
            return annotations

    def rules_fired_in_file(self, task):
        datafile_key, filepath, annotations_filepath = task
        file_cutoff = 100
        start = timer()
        file_dataframe = file_utilities.get_dataframe(filepath, file_cutoff)
        try:
            with open(annotations_filepath) as f:
                annotations = json.load(f)
        except:
            annotations = None
        try:
            lines_in_file = len(file_dataframe)

            bottom_boundary = file_dataframe.shape[0] - 1  # initialize
            # if assume_multi_tables==False:
            #     if 'tables' in annotations.keys():
            #         for table in annotations['tables']:
            #             if 'data_start' in table.keys():
            #                 bottom_boundary = table['bottom_boundary']
            #             break

            file_dataframe = file_dataframe.loc[:bottom_boundary]
            file_dataframe_trimmed = file_dataframe.copy()

            if self.parameters.max_attributes != None:
                max_attributes = self.parameters.max_attributes
                if self.parameters.ignore_left != None:
                    max_attributes = (
                        self.parameters.max_attributes + self.parameters.ignore_left
                    )
                slice_idx = min(max_attributes, file_dataframe.shape[1]) + 1
                file_dataframe_trimmed = file_dataframe.iloc[:, :slice_idx]

            signatures = TableSignatures(
                file_dataframe_trimmed, self.parameters.outlier_sensitive
            )

            data_rules_fired, not_data_rules_fired = collect_dataframe_rules(
                file_dataframe_trimmed, self, signatures
            )
            (
                pat_line_datapoints,
                pat_cell_datapoints,
                pat_data_line_rules,
                pat_not_data_line_rules,
                pat_data_cell_rules,
                pat_not_data_cell_rules,
                lines_in_sample,
            ) = save_training_data(
                datafile_key,
                file_dataframe_trimmed,
                annotations,
                data_rules_fired,
                not_data_rules_fired,
                self,
            )

            end = timer()
            processing_time = end - start
            return (
                datafile_key,
                pat_line_datapoints,
                pat_cell_datapoints,
                pat_data_line_rules,
                pat_not_data_line_rules,
                pat_data_cell_rules,
                pat_not_data_cell_rules,
                data_rules_fired,
                not_data_rules_fired,
                lines_in_file,
                lines_in_sample,
                processing_time,
            )
        except Exception:
            return Exception(
                "Err on item {}".format(task)
                #  + os.linesep + traceback.format_exc()
            )


def convert_predictions(
    predictions,
    blank_lines,
    last_line_processed,
    file_num_columns,
    file_max_columns_processed,
):
    annotations = {}
    annotations["blanklines"] = blank_lines
    annotations["lines_processed"] = last_line_processed
    annotations["columns_in_file"] = file_num_columns
    annotations["columns_in_file_considered"] = file_max_columns_processed
    annotations["tables"] = []
    for key in predictions.keys():
        table = dict()

        table["table_counter"] = int(key)
        table["top_boundary"] = int(predictions[key]["top_boundary"])
        table["bottom_boundary"] = int(predictions[key]["bottom_boundary"])
        table["data_start"] = int(predictions[key]["data_start"])
        table["data_end"] = int(predictions[key]["data_end"])
        table["header"] = [int(i) for i in predictions[key]["header"]]
        table["footnotes"] = [int(i) for i in predictions[key]["footnotes"]]
        table["subheaders"] = [
            int(i) for i in list(predictions[key]["subheader_scope"].keys())
        ]
        table["confidence"] = {
            "body_start": float(
                predictions[key]["fdl_confidence"]["avg_majority_confidence"]
            ),
            "body_end": float(predictions[key]["data_end_confidence"]),
            "body": float(
                combined_table_confidence(
                    predictions[key]["fdl_confidence"]["avg_majority_confidence"],
                    predictions[key]["data_end_confidence"],
                )
            ),
        }
        table["columns"] = predictions[key]["columns"]
        annotations["tables"].append(table)
    return annotations


def save_training_data(
    crawl_datafile_key,
    file_dataframe_trimmed,
    annotations,
    data_rules_fired,
    not_data_rules_fired,
    pat_model,
):
    lines_in_sample = 0
    ### LINE###
    pat_line_datapoints_attribute_names = [
        "crawl_datafile_key",
        "line_index",
        "label",
        "all_summaries_empty",
    ]

    pat_line_datapoints_attribute_values = []

    pat_data_line_rules_attribute_names = [
        "crawl_datafile_key",
        "line_index",
        "label",
        "undersample",
    ]

    for rule in pat_model.fuzzy_rules["line"]["data"].keys():
        pat_data_line_rules_attribute_names.append(rule.lower())

    pat_data_line_rules_attribute_values = []

    pat_not_data_line_rules_attribute_names = [
        "crawl_datafile_key",
        "line_index",
        "label",
        "undersample",
    ]

    for rule in pat_model.fuzzy_rules["line"]["not_data"].keys():
        pat_not_data_line_rules_attribute_names.append(str(rule.lower()))

    pat_not_data_line_rules_attribute_values = []

    ### CELL ###
    pat_cell_datapoints_attribute_names = [
        "crawl_datafile_key",
        "line_index",
        "column_index",
        "label",
    ]

    pat_cell_datapoints_attribute_values = []

    pat_data_cell_rules_attribute_names = [
        "crawl_datafile_key",
        "line_index",
        "column_index",
        "label",
        "aggregate",
        "summary_strength",
        "null_equivalent",
        "undersample",
    ]

    pat_data_cell_rules_attribute_values = []

    for rule in pat_model.fuzzy_rules["cell"]["data"].keys():
        pat_data_cell_rules_attribute_names.append(rule.lower())
    # data_cell_rules = pd.DataFrame(columns=pat_data_cell_rules_attribute_names)

    pat_not_data_cell_rules_attribute_names = [
        "crawl_datafile_key",
        "line_index",
        "column_index",
        "label",
        "disagreement_summary_strength",
        "undersample",
    ]
    for rule in pat_model.fuzzy_rules["cell"]["not_data"].keys():
        pat_not_data_cell_rules_attribute_names.append(rule.lower())
    # not_data_cell_rules = pd.DataFrame(columns=pat_not_data_cell_rules_attribute_names)
    pat_not_data_cell_rules_attribute_values = []

    top_boundary = 0
    data_indexes = []
    header_indexes = []
    footnotes = []
    blank_lines = []
    sub_headers = []
    if "tables" in annotations.keys():
        # print(f'\n\n\n----------------------------------------------\n\n\n- file_counter={file_counter}\n- crawl_datafile_key={crawl_datafile_key}\n- filepath={filepath}\n')

        for table in annotations["tables"]:
            if "data_start" in table.keys():
                table_counter = table["table_counter"]
                data_indexes = table["data_indexes"]
                header_indexes = table["header"]
                top_boundary = table["top_boundary"]
                bottom_boundary = table["bottom_boundary"]
                footnotes = table["footnotes"]
                sub_headers = table["subheaders"]

                if "blanklines" in table.keys():
                    blank_lines = table["blanklines"]
                try:
                    for line_index in data_rules_fired.keys():
                        pat_data_line_rules_fired = []
                        pat_not_data_line_rules_fired = []

                        if line_index in blank_lines:
                            row_class = "BLANK"
                        elif (
                            len(header_indexes) > 0 and line_index < header_indexes[0]
                        ) or (
                            len(header_indexes) == 0
                            and len(data_indexes) > 0
                            and line_index < data_indexes[0]
                        ):
                            row_class = "CONTEXT"
                        elif line_index in header_indexes:
                            row_class = "HEADER"
                        elif line_index in data_indexes:
                            row_class = "DATA"
                        elif line_index in footnotes:
                            row_class = "FOOTNOTE"
                        elif line_index in sub_headers:
                            row_class = "SUBHEADER"
                        else:
                            row_class = "OTHER"

                        for column_label in file_dataframe_trimmed.columns:
                            pat_data_cell_rules_fired = []
                            pat_not_data_cell_rules_fired = []
                            undersample = False
                            if (
                                (
                                    "tables" in annotations.keys()
                                    and table_counter == 1
                                    and len(annotations["tables"]) > 0
                                )
                                and column_label >= pat_model.parameters.ignore_left
                                and (
                                    len(data_indexes) > 0
                                    and data_indexes[0]
                                    + pat_model.parameters.undersample_data_limit
                                    > line_index
                                )
                                and (line_index < file_dataframe_trimmed.shape[0] - 1)
                            ):
                                undersample = True

                            cell_class = row_class
                            if str(
                                file_dataframe_trimmed.loc[line_index, column_label]
                            ).lower() in ["" or "nan"]:
                                cell_class = "BLANK"

                            pat_cell_datapoints_attribute_values.append(
                                (
                                    crawl_datafile_key,
                                    line_index,
                                    column_label,
                                    cell_class,
                                )
                            )
                            for rule in pat_model.fuzzy_rules["cell"]["data"].keys():
                                rule_fired = False
                                if (
                                    rule
                                    in data_rules_fired[line_index][column_label][
                                        "agreements"
                                    ]
                                ):
                                    rule_fired = True
                                pat_data_cell_rules_fired.append(rule_fired)
                            is_aggregate = data_rules_fired[line_index][column_label][
                                "aggregate"
                            ]
                            summary_strength = data_rules_fired[line_index][
                                column_label
                            ]["summary_strength"]
                            is_null_equivalent = data_rules_fired[line_index][
                                column_label
                            ]["null_equivalent"]
                            pat_data_cell_rules_attribute_values.append(
                                (
                                    crawl_datafile_key,
                                    line_index,
                                    column_label,
                                    cell_class,
                                    is_aggregate,
                                    summary_strength,
                                    is_null_equivalent,
                                    undersample,
                                )
                                + tuple(pat_data_cell_rules_fired)
                            )
                            for rule in pat_model.fuzzy_rules["cell"][
                                "not_data"
                            ].keys():
                                rule_fired = False
                                if (
                                    rule
                                    in not_data_rules_fired[line_index][column_label][
                                        "disagreements"
                                    ]
                                ):
                                    rule_fired = True
                                pat_not_data_cell_rules_fired.append(rule_fired)
                            disagreement_summary_strength = not_data_rules_fired[
                                line_index
                            ][column_label]["disagreement_summary_strength"]
                            pat_not_data_cell_rules_attribute_values.append(
                                (
                                    crawl_datafile_key,
                                    line_index,
                                    column_label,
                                    cell_class,
                                    disagreement_summary_strength,
                                    undersample,
                                )
                                + tuple(pat_not_data_cell_rules_fired)
                            )

                        undersample = False
                        if (
                            (
                                "tables" in annotations.keys()
                                and table_counter == 1
                                and len(annotations["tables"]) > 0
                            )
                            and (
                                len(data_indexes) > 0
                                and data_indexes[0]
                                + pat_model.parameters.undersample_data_limit
                                > line_index
                            )
                            and (line_index < file_dataframe_trimmed.shape[0] - 1)
                        ):
                            undersample = True
                            lines_in_sample += 1
                        else:  ################ ADDED
                            break  ################
                        # flag which DATA line rules fired
                        for rule in pat_model.fuzzy_rules["line"]["data"].keys():
                            rule_fired = False
                            if rule in data_rules_fired[line_index]["line"]:
                                rule_fired = True
                            pat_data_line_rules_fired.append(rule_fired)

                        pat_data_line_rules_attribute_values.append(
                            (crawl_datafile_key, line_index, row_class, undersample)
                            + tuple(pat_data_line_rules_fired)
                        )
                        # print(f'pat_data_line_rules_attribute_values={pat_data_line_rules_attribute_values}')
                        # input()
                        # flag which NOT DATA line rules fired
                        for rule in pat_model.fuzzy_rules["line"]["not_data"].keys():
                            rule_fired = False
                            if rule in not_data_rules_fired[line_index]["line"]:
                                rule_fired = True
                            pat_not_data_line_rules_fired.append(rule_fired)

                        pat_not_data_line_rules_attribute_values.append(
                            (crawl_datafile_key, line_index, row_class, undersample)
                            + tuple(pat_not_data_line_rules_fired)
                        )
                        all_summaries_empty = data_rules_fired[line_index][
                            "all_summaries_empty"
                        ]
                        pat_line_datapoints_attribute_values.append(
                            (
                                crawl_datafile_key,
                                line_index,
                                row_class,
                                all_summaries_empty,
                            )
                        )
                except Exception as e:
                    print(f"Exception in crawl_datafile_key={crawl_datafile_key}")
                    print(f"filepath={filepath}")
                    traceback.print_exc()
                    traceback.print_stack()
            break

    pat_line_datapoints = pd.DataFrame(
        pat_line_datapoints_attribute_values,
        columns=pat_line_datapoints_attribute_names,
    )
    pat_cell_datapoints = pd.DataFrame(
        pat_cell_datapoints_attribute_values,
        columns=pat_cell_datapoints_attribute_names,
    )

    pat_data_line_rules = pd.DataFrame(
        pat_data_line_rules_attribute_values,
        columns=pat_data_line_rules_attribute_names,
    )
    pat_not_data_line_rules = pd.DataFrame(
        pat_not_data_line_rules_attribute_values,
        columns=pat_not_data_line_rules_attribute_names,
    )
    pat_data_cell_rules = pd.DataFrame(
        pat_data_cell_rules_attribute_values,
        columns=pat_data_cell_rules_attribute_names,
    )
    pat_not_data_cell_rules = pd.DataFrame(
        pat_not_data_cell_rules_attribute_values,
        columns=pat_not_data_cell_rules_attribute_names,
    )

    for rule in pat_model.fuzzy_rules["line"]["data"].keys():
        pat_data_line_rules[rule.lower()] = pat_data_line_rules[rule.lower()].astype(
            "bool"
        )

    for rule in pat_model.fuzzy_rules["line"]["not_data"].keys():
        pat_not_data_line_rules[rule.lower()] = pat_not_data_line_rules[
            rule.lower()
        ].astype("bool")

    for rule in pat_model.fuzzy_rules["cell"]["data"].keys():
        pat_data_cell_rules[rule.lower()] = pat_data_cell_rules[rule.lower()].astype(
            "bool"
        )

    for rule in pat_model.fuzzy_rules["cell"]["not_data"].keys():
        pat_not_data_cell_rules[rule.lower()] = pat_not_data_cell_rules[
            rule.lower()
        ].astype("bool")

    pat_data_cell_rules["crawl_datafile_key"] = pat_data_cell_rules[
        "crawl_datafile_key"
    ].astype("int")
    pat_data_cell_rules["line_index"] = pat_data_cell_rules["line_index"].astype("int")
    pat_data_cell_rules["column_index"] = pat_data_cell_rules["column_index"].astype(
        "int"
    )
    pat_data_cell_rules["summary_strength"] = pat_data_cell_rules[
        "summary_strength"
    ].astype("int")
    pat_data_cell_rules["aggregate"] = pat_data_cell_rules["aggregate"].astype("bool")
    pat_data_cell_rules["null_equivalent"] = pat_data_cell_rules[
        "null_equivalent"
    ].astype("bool")
    pat_data_cell_rules["undersample"] = pat_data_cell_rules["undersample"].astype(
        "bool"
    )

    pat_not_data_cell_rules["crawl_datafile_key"] = pat_not_data_cell_rules[
        "crawl_datafile_key"
    ].astype("int")
    pat_not_data_cell_rules["line_index"] = pat_not_data_cell_rules[
        "line_index"
    ].astype("int")
    pat_not_data_cell_rules["column_index"] = pat_not_data_cell_rules[
        "column_index"
    ].astype("int")
    pat_not_data_cell_rules["disagreement_summary_strength"] = pat_not_data_cell_rules[
        "disagreement_summary_strength"
    ].astype("int")
    pat_not_data_cell_rules["undersample"] = pat_not_data_cell_rules[
        "undersample"
    ].astype("bool")

    pat_data_line_rules["crawl_datafile_key"] = pat_data_line_rules[
        "crawl_datafile_key"
    ].astype("int")
    pat_data_line_rules["line_index"] = pat_data_line_rules["line_index"].astype("int")
    pat_data_line_rules["undersample"] = pat_data_line_rules["undersample"].astype(
        "bool"
    )

    pat_not_data_line_rules["crawl_datafile_key"] = pat_not_data_line_rules[
        "crawl_datafile_key"
    ].astype("int")
    pat_not_data_line_rules["line_index"] = pat_not_data_line_rules[
        "line_index"
    ].astype("int")
    pat_not_data_line_rules["undersample"] = pat_not_data_line_rules[
        "undersample"
    ].astype("bool")

    pat_cell_datapoints["crawl_datafile_key"] = pat_cell_datapoints[
        "crawl_datafile_key"
    ].astype("int")
    pat_cell_datapoints["line_index"] = pat_cell_datapoints["line_index"].astype("int")
    pat_cell_datapoints["column_index"] = pat_cell_datapoints["column_index"].astype(
        "int"
    )

    pat_line_datapoints["crawl_datafile_key"] = pat_line_datapoints[
        "crawl_datafile_key"
    ].astype("int")
    pat_line_datapoints["line_index"] = pat_line_datapoints["line_index"].astype("int")
    pat_line_datapoints["all_summaries_empty"] = pat_line_datapoints[
        "all_summaries_empty"
    ].astype("bool")

    return (
        pat_line_datapoints,
        pat_cell_datapoints,
        pat_data_line_rules,
        pat_not_data_line_rules,
        pat_data_cell_rules,
        pat_not_data_cell_rules,
        lines_in_sample,
    )


def pat_rule_worker(task):
    start = timer()
    top_level_dir, file_counter, file_object, pat_classifier, db_cred = task
    # if file_counter%100==0:
    #     print(f'file_counter={file_counter}')
    max_attributes = pat_classifier.parameters.max_attributes
    crawl_datafile_key = file_object[0]
    groundtruth_key = file_object[1]
    annotations = file_object[2]
    filepath = file_object[3]
    failure = file_object[4]

    filepath = os.path.join(top_level_dir, filepath)
    file_dataframe = file_utilities.get_dataframe(filepath, 100)
    lines_in_file = len(file_dataframe)

    bottom_boundary = file_dataframe.shape[0] - 1  # initialize
    # if assume_multi_tables==False:
    #     if 'tables' in annotations.keys():
    #         for table in annotations['tables']:
    #             if 'data_start' in table.keys():
    #                 bottom_boundary = table['bottom_boundary']
    #             break

    file_dataframe = file_dataframe.loc[:bottom_boundary]
    file_dataframe_trimmed = file_dataframe.copy()

    if pat_classifier.parameters.max_attributes != None:
        max_attributes = pat_classifier.parameters.max_attributes
        if pat_classifier.parameters.ignore_left != None:
            max_attributes = (
                pat_classifier.parameters.max_attributes
                + pat_classifier.parameters.ignore_left
            )
        slice_idx = min(max_attributes, file_dataframe.shape[1]) + 1
        file_dataframe_trimmed = file_dataframe.iloc[:, :slice_idx]

    # ignore_left = args.ignore_left
    # if args.ignore_left >= file_dataframe_trimmed.shape[1]: # trying to ignore more columns than the file actually has!
    #     ignore_left = file_dataframe_trimmed.shape[1]-1 # ignore all except the last one
    # else:
    #     file_dataframe_trimmed = file_dataframe_trimmed[:ignore_left]

    signatures = TableSignatures(
        file_dataframe_trimmed, pat_classifier.parameters.outlier_sensitive
    )

    data_rules_fired, not_data_rules_fired = collect_dataframe_rules(
        file_dataframe_trimmed, pat_classifier, signatures
    )
    (
        pat_line_datapoints,
        pat_cell_datapoints,
        pat_data_line_rules,
        pat_not_data_line_rules,
        pat_data_cell_rules,
        pat_not_data_cell_rules,
        lines_in_sample,
    ) = save_training_data(
        crawl_datafile_key,
        file_dataframe_trimmed,
        annotations,
        data_rules_fired,
        not_data_rules_fired,
        pat_classifier,
    )

    end = timer()
    processing_time = end - start
    return (
        crawl_datafile_key,
        pat_line_datapoints,
        pat_cell_datapoints,
        pat_data_line_rules,
        pat_not_data_line_rules,
        pat_data_cell_rules,
        pat_not_data_cell_rules,
        data_rules_fired,
        not_data_rules_fired,
        lines_in_file,
        lines_in_sample,
        processing_time,
    )


def merge_tables(table_head, table_tail):
    # update the previous table
    table_head["data_end"] = table_tail["data_end"]
    table_head["subheader_scope"] = {
        **table_head["subheader_scope"],
        **table_tail["subheader_scope"],
    }
    for subheader in list(
        set(range(table_tail["top_boundary"], table_tail["data_start"]))
        - set(table_tail["header"])
    ):
        table_head["subheader_scope"][subheader] = dict()
    table_head["aggregation_scope"] = {
        **table_head["aggregation_scope"],
        **table_tail["aggregation_scope"],
    }
    return table_head


def discover_next_table(
    csv_file,
    file_offset,
    table_counter,
    data_rules_fired,
    not_data_rules_fired,
    blank_lines,
    headers_discovered,
    signatures,
    model,
):
    # print('\n\n --> [METHOD] pat.discover_next_table >>')
    parameters = model.parameters
    # print(f'\nDiscovering table {table_counter}:\n')
    discovered_table = None
    csv_file = csv_file.loc[file_offset:]
    if csv_file.empty:
        return discovered_table

    # print(f'{csv_file}\nPress enter...')

    start = timer()
    data_line_confidences, not_data_line_confidences = get_class_confidences(
        csv_file, data_rules_fired, not_data_rules_fired, model.fuzzy_rules, parameters
    )
    end = timer()

    start = timer()
    (
        combined_data_line_confidences,
        line_predictions,
    ) = predict_combined_data_confidences(
        csv_file,
        data_line_confidences,
        not_data_line_confidences,
        parameters.max_candidates,
    )
    end = timer()

    pat_first_data_line, first_data_line_combined_data_predictions = predict_fdl(
        csv_file,
        line_predictions,
        parameters.markov_approximation_probabilities,
        parameters.markov_model,
        2,
        parameters.combined_label_weight,
    )

    header_predictions = {}
    header_predictions["avg_confidence"] = first_data_line_combined_data_predictions[
        "avg_confidence"
    ]
    header_predictions["min_confidence"] = first_data_line_combined_data_predictions[
        "min_confidence"
    ]
    header_predictions["softmax"] = first_data_line_combined_data_predictions["softmax"]
    header_predictions[
        "prod_softmax_prior"
    ] = first_data_line_combined_data_predictions["prod_softmax_prior"]

    predicted_pat_header_indexes = []
    candidate_pat_sub_headers = []
    predicted_pat_sub_headers = []

    if pat_first_data_line >= 0:
        (
            predicted_pat_header_indexes,
            candidate_pat_sub_headers,
        ) = predict_header_indexes(csv_file, pat_first_data_line, table_counter)
        candidate_pat_sub_headers.sort()
        for h in predicted_pat_header_indexes:
            headers_discovered[h] = ",".join(csv_file.loc[h].apply(str).tolist())

        if len(candidate_pat_sub_headers) > 0:
            data_section_start = min(
                min(candidate_pat_sub_headers), pat_first_data_line
            )
        else:
            data_section_start = pat_first_data_line

        predicted_pat_data_lines = []

        header_dataframe = csv_file.loc[predicted_pat_header_indexes]

        #     # First data line predicted
        #     #############################   END  ###############################################

        #     ############################## START ###############################################
        #     # Predict Last data line

        cand_data = csv_file.loc[data_section_start:]
        aggregation_rows, subheader_scope = predict_subheaders_new(
            csv_file,
            cand_data,
            candidate_pat_sub_headers,
            blank_lines,
            predicted_pat_header_indexes,
            model,
        )

        predicted_pat_sub_headers = list(subheader_scope.keys())
        pat_last_data_line = pat_first_data_line
        (
            pat_last_data_line,
            bottom_boundary_confidence,
        ) = predict_last_data_line_top_down(
            csv_file,
            pat_first_data_line,
            data_line_confidences,
            not_data_line_confidences,
            model,
            subheader_scope,
            aggregation_rows,
            blank_lines,
            headers_discovered,
            signatures,
            data_rules_fired,
            not_data_rules_fired,
        )

        for blank_line_idx in blank_lines:
            if blank_line_idx in predicted_pat_data_lines:
                predicted_pat_data_lines.remove(blank_line_idx)

        discovered_table = {}
        discovered_table["top_boundary"] = file_offset
        discovered_table["bottom_boundary"] = len(csv_file) - 1
        discovered_table["data_start"] = data_section_start
        discovered_table["data_end_confidence"] = bottom_boundary_confidence[
            "confidence"
        ]

        table_dataframe = pd.concat(
            [header_dataframe, csv_file.loc[pat_first_data_line:pat_last_data_line]]
        )
        null_columns = table_dataframe.columns[table_dataframe.isna().all()].tolist()
        table_dataframe = table_dataframe.drop(null_columns, axis=1)
        column_names = name_table_columns(
            table_dataframe.loc[predicted_pat_header_indexes]
        )
        discovered_table["columns"] = column_names

        discovered_table["fdl_confidence"] = dict()
        discovered_table["fdl_confidence"]["avg_majority_confidence"] = float(
            first_data_line_combined_data_predictions["avg_confidence"]["confidence"]
        )
        discovered_table["fdl_confidence"]["avg_difference"] = float(
            first_data_line_combined_data_predictions["avg_confidence"]["difference"]
        )
        discovered_table["fdl_confidence"]["avg_confusion_index"] = float(
            first_data_line_combined_data_predictions["avg_confidence"][
                "confusion_index"
            ]
        )
        discovered_table["fdl_confidence"]["softmax"] = float(
            first_data_line_combined_data_predictions["softmax"]
        )
        discovered_table["header"] = predicted_pat_header_indexes
        predicted_pat_data_lines = list(
            range(data_section_start, pat_last_data_line + 1)
        )
        discovered_table["data_end"] = pat_last_data_line
        discovered_table["footnotes"] = []

    if pat_first_data_line >= 0:
        #     # Last data line predicted
        #     #############################   END  ###############################################

        cand_data = csv_file.loc[data_section_start:pat_last_data_line]
        candidate_pat_sub_headers = sorted(
            list(
                set(predicted_pat_sub_headers).intersection(
                    set(predicted_pat_data_lines)
                )
            )
        )
        # candidate_pat_sub_headers = list(set(predicted_pat_sub_headers+list(cand_data.index[cand_data.iloc[:,1:].isnull().all(1)])))
        # input(f'\n\t -> 2 candidate_pat_sub_headers={candidate_pat_sub_headers}')

        aggregation_scope, subheader_scope = predict_subheaders(
            csv_file,
            cand_data,
            candidate_pat_sub_headers,
            blank_lines,
            predicted_pat_header_indexes,
            model,
        )
        predicted_pat_sub_headers = subheader_scope.keys()
        # print(f'\t 2 predicted_pat_sub_headers={predicted_pat_sub_headers}')

        discovered_table["subheader_scope"] = subheader_scope
        discovered_table["aggregation_scope"] = aggregation_scope

        for subheaders_idx in predicted_pat_sub_headers:
            if subheaders_idx in predicted_pat_data_lines:
                predicted_pat_data_lines.remove(subheaders_idx)
        # input(f'\npredicted_pat_sub_headers={predicted_pat_sub_headers}\n')
        discovered_table["footnotes"] = []
    return discovered_table


def predict_subheaders(
    csv_file, cand_data, predicted_pat_sub_headers, pat_blank_lines, pat_headers, model
):
    ignore_rules = model.ignore_rules
    fuzzy_rules = model.fuzzy_rules
    args = model.parameters

    cand_subhead_indexes = predicted_pat_sub_headers
    cand_subhead_indexes.sort()

    candidate_subheaders = dict()
    subheader_scope = dict()
    certain_data_indexes = list(cand_data.index)
    aggregation_rows = dict()
    first_column_data_values = []

    for row in cand_data.loc[certain_data_indexes].itertuples():
        first_value = str(row[1]).strip()
        first_value_tokens = first_value.lower().split()
        for aggregation_phrase in pat_util.aggregation_functions:
            agg_index = first_value.lower().find(aggregation_phrase[0])
            if agg_index > -1:
                aggregation_rows[row.Index] = {}
                aggregation_rows[row.Index]["value"] = first_value
                aggregation_rows[row.Index][
                    "aggregation_function"
                ] = aggregation_phrase[1]
                aggregation_rows[row.Index]["aggregation_phrase"] = aggregation_phrase[
                    0
                ]
                aggregation_rows[row.Index]["aggregation_label"] = (
                    first_value[:agg_index]
                    + first_value[agg_index + len(aggregation_phrase[0]) :]
                )
                break

        if (
            row.Index not in aggregation_rows.keys()
            and first_value.lower() not in pat_util.null_equivalent_values
            and row.Index not in cand_subhead_indexes
        ):
            first_column_data_values.append(first_value)

    certain_data_indexes = list(
        set(certain_data_indexes)
        - set(aggregation_rows.keys())
        - set(cand_subhead_indexes)
    )

    for row in csv_file.loc[cand_subhead_indexes].itertuples():
        first_value = str(row[1]).strip()
        if (
            first_value.lower() not in ["nan", "none", ""]
            and row.Index not in aggregation_rows.keys()
        ):
            candidate_subheaders[row.Index] = first_value

    cand_subhead_indexes = list(candidate_subheaders.keys())
    (
        aggregation_rows,
        certain_data_indexes,
        predicted_pat_sub_headers,
        cand_subhead_indexes,
        subheader_scope,
    ) = discover_aggregation_scope(
        csv_file.loc[: cand_data.index[-1]],
        aggregation_rows,
        candidate_subheaders,
        predicted_pat_sub_headers,
        certain_data_indexes,
        pat_headers,
    )

    if cand_subhead_indexes != None and len(cand_subhead_indexes) > 0:
        first_column_value_patterns = []
        first_column_value_symbols = []
        first_column_value_cases = []
        first_column_value_token_lengths = []
        first_column_value_char_lengths = []
        first_column_value_tokens = []
        first_column_all_patterns_numeric = []

        for value in first_column_data_values:
            (
                pattern,
                symbols,
                case,
                value_num_tokens,
                value_num_chars,
            ) = pat_util.generate_pattern_symbols_and_case(
                str(value).strip(), args.outlier_sensitive
            )
            first_column_value_patterns.append(pattern)
            first_column_value_symbols.append(symbols)
            first_column_value_cases.append(case)
            first_column_value_token_lengths.append(value_num_tokens)
            first_column_value_char_lengths.append(value_num_chars)
            first_column_value_tokens.append(
                [
                    i
                    for i in (str(value).strip().lower()).split()
                    if i not in stop
                    and all(j.isalpha() or j in string.punctuation for j in i)
                    and i not in pat_util.null_equivalent_values
                ]
            )
            first_column_all_patterns_numeric.append(
                table_classifier_utilities.eval_numeric_pattern(pattern)
            )
        if args.normalize_decimals == True:
            (
                first_column_value_patterns,
                first_column_value_symbols,
            ) = pat_util.normalize_decimals_numbers(
                first_column_value_patterns, first_column_value_symbols
            )

        (
            value_pattern_summary,
            value_chain_consistent,
        ) = pat_util.generate_pattern_summary(first_column_value_patterns)
        summary_strength = sum(1 for x in first_column_value_patterns if len(x) > 0)
        bw_patterns = [
            list(reversed(pattern)) for pattern in first_column_value_patterns
        ]
        value_pattern_BW_summary, _ = pat_util.generate_pattern_summary(bw_patterns)

        # input(f'value_pattern_BW_summary={value_pattern_BW_summary}')

        value_symbol_summary = pat_util.generate_symbol_summary(
            first_column_value_symbols
        )
        case_summary = pat_util.generate_case_summary(first_column_value_cases)
        length_summary = pat_util.generate_length_summary(
            first_column_value_char_lengths
        )
        all_patterns_numeric, _ = pat_util.generate_all_numeric_sig_pattern(
            first_column_all_patterns_numeric,
            [len(t) for t in first_column_value_patterns],
        )

        candidate_tokens = set()
        if len(first_column_value_tokens) > 0:
            candidate_tokens = set(
                [t for t in first_column_value_tokens[0] if any(c.isalpha() for c in t)]
            )
        candidate_count_for_value = 0
        if len(first_column_data_values) > 2:
            candidate_count_for_value = np.count_nonzero(
                first_column_data_values[
                    2 : min(args.max_summary_strength, len(first_column_data_values))
                ]
                == str(value).strip()
            )

        partof_multiword_value_repeats = dict()
        for part in candidate_tokens:
            partof_multiword_value_repeats[part] = 0
            for value_tokens in first_column_value_tokens:
                if part in value_tokens:
                    partof_multiword_value_repeats[part] += 1
        consistent_symbol_sets, _ = is_consistent_symbol_sets(
            first_column_value_symbols
        )
        data_rules_fired = {}
        data_rules_fired[1] = {}
        data_rules_fired[1][0] = {}
        data_rules_fired[1][0]["agreements"] = []
        data_rules_fired[1][0]["null_equivalent"] = False
        for rule in fuzzy_rules["cell"]["data"].keys():
            rule_fired = False
            # Don't bother looking for agreements if there are no patterns
            non_empty_patterns = 0
            if (
                rule not in ignore_rules["cell"]["data"]
                and len(first_column_value_patterns) > 0
            ):
                for pattern in first_column_value_patterns:
                    if pattern != []:
                        non_empty_patterns += 1

                # there is no point calculating agreement over one value, a single value always agrees with itself.
                # in addition, many tables have bilingual headers, so agreement between two header values is very common, require nij>=3
                if len(first_column_value_patterns) >= 2 and non_empty_patterns >= 2:
                    rule_fired = eval_data_cell_rule(
                        rule,
                        first_column_data_values,
                        first_column_value_tokens,
                        value_pattern_summary,
                        value_chain_consistent,
                        value_pattern_BW_summary,
                        value_symbol_summary,
                        first_column_value_symbols,
                        first_column_value_patterns,
                        case_summary,
                        candidate_count_for_value,
                        partof_multiword_value_repeats,
                        candidate_tokens,
                        consistent_symbol_sets,
                        all_patterns_numeric,
                        len(first_column_data_values),
                    )
                    # if rule_fired and "_REPEATS_" not in rule and rule not in ['FW_SUMMARY_D', 'FW_TWO_OR_MORE_OR_MORE_NO_SPACE', 'FW_TWO_OR_MORE_OR_MORE_NO_SPACE_FIRST_TWO','BW_TWO_OR_MORE_OR_MORE_NO_SPACE', 'BW_TWO_OR_MORE_OR_MORE_NO_SPACE_FIRST_TWO', 'BW_LENGTH_4PLUS', 'FW_LENGTH_4PLUS']:
                    if rule_fired and "_REPEATS_" not in rule:
                        data_rules_fired[1][0]["agreements"].append(rule)

        # input(f"\nfirst_column_data_cell_rules_fired={data_rules_fired[1][0]['agreements']}")

        for row in csv_file.loc[cand_subhead_indexes].itertuples():
            first_value = str(row[1]).strip()
            if first_value.lower() in ["", "nan", "none", "null"]:
                continue
            if first_value in first_column_data_values:
                continue
            if row.Index - 1 in pat_blank_lines or row.Index - 1 in pat_headers:
                predicted_pat_sub_headers.append(row.Index)
            else:

                value_tokens = first_value.lower().split()
                (
                    pattern,
                    symbols,
                    case,
                    value_num_tokens,
                    value_num_chars,
                ) = pat_util.generate_pattern_symbols_and_case(
                    str(first_value).strip(), args.outlier_sensitive
                )
                if args.normalize_decimals == True:
                    (
                        column_patterns,
                        column_symbols,
                    ) = pat_util.normalize_decimals_numbers(
                        [pattern] + first_column_value_patterns,
                        [symbols] + first_column_value_symbols,
                    )
                (
                    value_pattern_summary,
                    value_chain_consistent,
                ) = pat_util.generate_pattern_summary(column_patterns)

                summary_strength = sum(1 for x in column_patterns if len(x) > 0)
                bw_patterns = [list(reversed(pattern)) for pattern in column_patterns]
                value_pattern_BW_summary, _ = pat_util.generate_pattern_summary(
                    bw_patterns
                )
                value_symbol_summary = pat_util.generate_symbol_summary(column_symbols)
                case_summary = pat_util.generate_case_summary(
                    [case] + first_column_value_cases
                )
                length_summary = pat_util.generate_length_summary(
                    [value_num_chars] + first_column_value_char_lengths
                )
                all_patterns_numeric, _ = pat_util.generate_all_numeric_sig_pattern(
                    [table_classifier_utilities.eval_numeric_pattern(pattern)]
                    + first_column_all_patterns_numeric,
                    [len(t) for t in column_patterns],
                )
                column_values = [first_value] + first_column_data_values
                column_tokens = [value_tokens] + first_column_value_tokens

                candidate_tokens = set(
                    [t for t in value_tokens if any(c.isalpha() for c in t)]
                )
                candidate_count_for_value = 0
                if len(column_values) > 2:
                    candidate_count_for_value = np.count_nonzero(
                        column_values[
                            2 : min(args.max_summary_strength, len(column_values))
                        ]
                        == str(value).strip()
                    )

                partof_multiword_value_repeats = dict()
                for part in candidate_tokens:
                    partof_multiword_value_repeats[part] = 0
                    for value_tokens in column_tokens:
                        if part in value_tokens:
                            partof_multiword_value_repeats[part] += 1

                consistent_symbol_sets, _ = is_consistent_symbol_sets(column_symbols)
                cand_subhead_data_cell_rules_fired = []
                data_rules_fired[0] = {}
                data_rules_fired[0][0] = {}
                data_rules_fired[0][0]["agreements"] = []
                data_rules_fired[0][0]["null_equivalent"] = False
                for rule in fuzzy_rules["cell"]["data"].keys():
                    rule_fired = False
                    non_empty_patterns = 0
                    if (
                        len(column_patterns) > 0
                        and first_value.lower() not in pat_util.null_equivalent_values
                    ):
                        for pattern in column_patterns:
                            if pattern != []:
                                non_empty_patterns += 1
                        if len(column_patterns) >= 2 and non_empty_patterns >= 2:
                            rule_fired = eval_data_cell_rule(
                                rule,
                                column_values,
                                column_tokens,
                                value_pattern_summary,
                                value_chain_consistent,
                                value_pattern_BW_summary,
                                value_symbol_summary,
                                column_symbols,
                                column_patterns,
                                case_summary,
                                candidate_count_for_value,
                                partof_multiword_value_repeats,
                                candidate_tokens,
                                consistent_symbol_sets,
                                all_patterns_numeric,
                                len(column_values),
                            )
                            if rule_fired == True and "_REPEATS_" not in rule:
                                data_rules_fired[0][0]["agreements"].append(rule)

                value_disagreements = []
                disagreement_summary_strength = summary_strength - 1
                if len(pattern) > 0:
                    repetitions_of_candidate = column_values[1:].count(first_value)
                    neighbor = ""
                    try:
                        neighbor = column_values[1]
                        repetitions_of_neighbor = column_values[2:].count(neighbor)
                    except:
                        repetitions_of_neighbor = 0

                    for rule in fuzzy_rules["cell"]["not_data"].keys():
                        rule_fired = False
                        if (
                            rule not in ignore_rules["cell"]["not_data"]
                            and disagreement_summary_strength > 0
                            and (
                                all_numbers(column_symbols) == False
                                or is_number(symbols) == False
                            )
                        ):
                            rule_fired = eval_not_data_cell_rule(
                                rule,
                                repetitions_of_candidate,
                                repetitions_of_neighbor,
                                neighbor,
                                value_pattern_summary,
                                value_pattern_BW_summary,
                                value_chain_consistent,
                                value_symbol_summary,
                                case_summary,
                                length_summary,
                                pattern,
                                symbols,
                                case,
                                value_num_chars,
                                disagreement_summary_strength,
                                data_rules_fired,
                                0,
                                0,
                            )
                            if rule_fired == True and "_REPEATS_" not in rule:
                                value_disagreements.append(rule)

                #######################################################################v######
                #  DATA value classification
                data_score = max_score(
                    data_rules_fired[0][0]["agreements"],
                    fuzzy_rules["cell"]["data"],
                    args.weight_lower_bound,
                )
                POPULATION_WEIGHT = 1 - (1 - args.p) ** (2 * summary_strength)
                if data_score != None:
                    if args.summary_population_factor:
                        cell_data_score = data_score * POPULATION_WEIGHT
                    else:
                        cell_data_score = data_score

                #######################################################################v######
                #  NOT DATA value classification
                not_data_score = max_score(
                    value_disagreements,
                    fuzzy_rules["cell"]["not_data"],
                    args.not_data_weight_lower_bound,
                )
                POPULATION_WEIGHT = 1 - (1 - args.p) ** (
                    2 * disagreement_summary_strength
                )
                if not_data_score != None:
                    if args.summary_population_factor:
                        cell_not_data_score = not_data_score * POPULATION_WEIGHT
                    else:
                        cell_not_data_score = not_data_score

                if (
                    cell_data_score > cell_not_data_score
                ):  # candidate subheader is definitely data, move along
                    continue

                if (
                    row.Index - 1 in predicted_pat_sub_headers
                    and row.Index - 2 in predicted_pat_sub_headers
                ):
                    continue

                if row.Index != cand_data.index[-1]:
                    predicted_pat_sub_headers.append(row.Index)

    # print(f'predicted_pat_sub_headers={predicted_pat_sub_headers}')
    for s_i, subheader in enumerate(predicted_pat_sub_headers):
        if subheader not in subheader_scope.keys():
            if s_i + 1 == len(predicted_pat_sub_headers):
                subheader_scope[subheader] = list(
                    range(subheader + 1, cand_data.index[-1] + 1)
                )
            else:
                next_s_i = s_i + 1
                while next_s_i < len(predicted_pat_sub_headers):
                    next_subh = predicted_pat_sub_headers[next_s_i]
                    if next_subh not in subheader_scope:
                        subheader_scope[subheader] = list(
                            range(subheader + 1, next_subh)
                        )
                        break
                    next_s_i += 1

    return aggregation_rows, subheader_scope


def predict_subheaders_new(
    csv_file, cand_data, predicted_pat_sub_headers, pat_blank_lines, pat_headers, model
):
    args = model.parameters
    fuzzy_rules = model.fuzzy_rules
    ignore_rules = model.ignore_rules

    cand_subhead_indexes = list(
        set(
            predicted_pat_sub_headers
            + list(cand_data.index[cand_data.iloc[:, 1:].isnull().all(1)])
        )
    )
    candidate_subheaders = {}
    subheader_scope = {}
    certain_data_indexes = list(cand_data.index)
    aggregation_rows = {}
    first_column_data_values = []

    # print(f'csv_file=\n{csv_file}\n')
    # print(f'cand_data=\n{cand_data}\n')

    for row in csv_file.loc[certain_data_indexes].itertuples():
        first_value = str(row[1]).strip()
        # input(f'row_{row.Index}: first_value={first_value}')
        first_value_tokens = first_value.lower().split()
        for aggregation_phrase in pat_util.aggregation_functions:
            agg_index = first_value.lower().find(aggregation_phrase[0])
            # print(f'{aggregation_phrase[0]} in {first_value.lower()}={aggregation_phrase[0] in first_value.lower()}')

            if agg_index > -1 and contains_number(row[1:]):
                aggregation_rows[row.Index] = {}
                aggregation_rows[row.Index]["value"] = first_value
                aggregation_rows[row.Index][
                    "aggregation_function"
                ] = aggregation_phrase[1]
                aggregation_rows[row.Index]["aggregation_phrase"] = aggregation_phrase[
                    0
                ]
                aggregation_rows[row.Index]["aggregation_label"] = (
                    first_value[:agg_index]
                    + first_value[agg_index + len(aggregation_phrase[0]) :]
                )
                break

        if (
            row.Index not in aggregation_rows.keys()
            and first_value.lower() not in pat_util.null_equivalent_values
            and row.Index not in cand_subhead_indexes
        ):
            first_column_data_values.append(first_value)

    certain_data_indexes = list(
        set(certain_data_indexes)
        - set(aggregation_rows.keys())
        - set(cand_subhead_indexes)
    )

    for row in csv_file.loc[cand_subhead_indexes].itertuples():
        first_value = str(row[1]).strip()
        if (
            first_value.lower() not in ["nan", "none", ""]
            and row.Index not in aggregation_rows.keys()
        ):
            candidate_subheaders[row.Index] = first_value

    cand_subhead_indexes = list(candidate_subheaders.keys())

    (
        aggregation_rows,
        certain_data_indexes,
        predicted_pat_sub_headers,
        cand_subhead_indexes,
        subheader_scope,
    ) = discover_aggregation_scope(
        csv_file,
        aggregation_rows,
        candidate_subheaders,
        predicted_pat_sub_headers,
        certain_data_indexes,
        pat_headers,
    )

    if cand_subhead_indexes != None and len(cand_subhead_indexes) > 0:
        first_column_value_patterns = []
        first_column_value_symbols = []
        first_column_value_cases = []
        first_column_value_token_lengths = []
        first_column_value_char_lengths = []
        first_column_value_tokens = []
        first_column_all_patterns_numeric = []

        # print(f'\ncand_subhead_indexes={cand_subhead_indexes}\n')
        # pp.pprint(first_column_data_values)
        # input()
        for value in first_column_data_values:
            (
                pattern,
                symbols,
                case,
                value_num_tokens,
                value_num_chars,
            ) = pat_util.generate_pattern_symbols_and_case(
                str(value).strip(), args.outlier_sensitive
            )
            first_column_value_patterns.append(pattern)
            first_column_value_symbols.append(symbols)
            first_column_value_cases.append(case)
            first_column_value_token_lengths.append(value_num_tokens)
            first_column_value_char_lengths.append(value_num_chars)
            first_column_value_tokens.append(
                [
                    i
                    for i in (str(value).strip().lower()).split()
                    if i not in stop
                    and all(j.isalpha() or j in string.punctuation for j in i)
                    and i not in pat_util.null_equivalent_values
                ]
            )
            first_column_all_patterns_numeric.append(
                table_classifier_utilities.eval_numeric_pattern(pattern)
            )

        if args.normalize_decimals == True:
            (
                first_column_value_patterns,
                first_column_value_symbols,
            ) = pat_util.normalize_decimals_numbers(
                first_column_value_patterns, first_column_value_symbols
            )

        (
            value_pattern_summary,
            value_chain_consistent,
        ) = pat_util.generate_pattern_summary(first_column_value_patterns)
        summary_strength = sum(1 for x in first_column_value_patterns if len(x) > 0)
        bw_patterns = [
            list(reversed(pattern)) for pattern in first_column_value_patterns
        ]
        value_pattern_BW_summary, _ = pat_util.generate_pattern_summary(bw_patterns)
        all_patterns_numeric, _ = pat_util.generate_all_numeric_sig_pattern(
            first_column_all_patterns_numeric,
            [len(t) for t in first_column_value_patterns],
        )
        value_symbol_summary = pat_util.generate_symbol_summary(
            first_column_value_symbols
        )
        case_summary = pat_util.generate_case_summary(first_column_value_cases)
        length_summary = pat_util.generate_length_summary(
            first_column_value_char_lengths
        )

        if len(first_column_value_tokens) > 0:
            candidate_tokens = set(
                [t for t in first_column_value_tokens[0] if any(c.isalpha() for c in t)]
            )
        else:
            candidate_tokens = set()
        if len(first_column_data_values) > 2:
            candidate_count_of_value = np.count_nonzero(
                first_column_data_values[
                    2 : min(args.max_summary_strength, len(first_column_data_values))
                ]
                == str(value).strip()
            )
        else:
            candidate_count_of_value = 0

        partof_multiword_value_repeats = dict()
        for part in candidate_tokens:
            partof_multiword_value_repeats[part] = 0
            for value_tokens in first_column_value_tokens:
                if part in value_tokens:
                    partof_multiword_value_repeats[part] += 1
        consistent_symbol_sets, _ = is_consistent_symbol_sets(
            first_column_value_symbols
        )
        data_rules_fired = {}
        data_rules_fired[1] = {}
        data_rules_fired[1][0] = {}
        data_rules_fired[1][0]["agreements"] = []
        data_rules_fired[1][0]["null_equivalent"] = False
        for rule in fuzzy_rules["cell"]["data"].keys():
            rule_fired = False

            # Don't bother looking for agreements if there are no patterns
            non_empty_patterns = 0
            if (
                rule not in ignore_rules["cell"]["data"]
                and len(first_column_value_patterns) > 0
            ):
                for pattern in first_column_value_patterns:
                    if pattern != []:
                        non_empty_patterns += 1

                # there is no point calculating agreement over one value, a single value always agrees with itself.
                # in addition, many tables have bilingual headers, so agreement between two header values is very common, require nij>=3
                if len(first_column_value_patterns) >= 2 and non_empty_patterns >= 2:
                    rule_fired = eval_data_cell_rule(
                        rule,
                        first_column_data_values,
                        first_column_value_tokens,
                        value_pattern_summary,
                        value_chain_consistent,
                        value_pattern_BW_summary,
                        value_symbol_summary,
                        first_column_value_symbols,
                        first_column_value_patterns,
                        case_summary,
                        candidate_count_of_value,
                        partof_multiword_value_repeats,
                        candidate_tokens,
                        consistent_symbol_sets,
                        all_patterns_numeric,
                        len(first_column_data_values),
                    )
                    if rule_fired and "_REPEATS_" not in rule:
                        data_rules_fired[1][0]["agreements"].append(rule)

        # input(f"\nfirst_column_data_cell_rules_fired={data_rules_fired[1][0]['agreements']}")

        for row in csv_file.loc[cand_subhead_indexes].itertuples():
            first_value = str(row[1]).strip()
            if first_value.lower() in ["", "nan", "none", "null"]:
                continue
            if first_value in first_column_data_values:
                continue
            if row.Index - 1 in pat_blank_lines or row.Index - 1 in pat_headers:
                predicted_pat_sub_headers.append(row.Index)
            else:

                value_tokens = first_value.lower().split()
                (
                    pattern,
                    symbols,
                    case,
                    value_num_tokens,
                    value_num_chars,
                ) = pat_util.generate_pattern_symbols_and_case(
                    str(first_value).strip(), args.outlier_sensitive
                )
                if args.normalize_decimals == True:
                    (
                        column_patterns,
                        column_symbols,
                    ) = pat_util.normalize_decimals_numbers(
                        [pattern] + first_column_value_patterns,
                        [symbols] + first_column_value_symbols,
                    )
                    # input(f'\nrow_{row.Index} {[pattern]+first_column_value_patterns}')
                    # print(f'column_patterns={column_patterns}')
                (
                    value_pattern_summary,
                    value_chain_consistent,
                ) = pat_util.generate_pattern_summary(column_patterns)

                # print(f'value_pattern_summary={value_pattern_summary}')
                summary_strength = sum(1 for x in column_patterns if len(x) > 0)
                bw_patterns = [list(reversed(pattern)) for pattern in column_patterns]
                value_pattern_BW_summary, _ = pat_util.generate_pattern_summary(
                    bw_patterns
                )
                value_symbol_summary = pat_util.generate_symbol_summary(column_symbols)
                case_summary = pat_util.generate_case_summary(
                    [case] + first_column_value_cases
                )
                length_summary = pat_util.generate_length_summary(
                    [value_num_chars] + first_column_value_char_lengths
                )
                all_patterns_numeric, _ = pat_util.generate_all_numeric_sig_pattern(
                    [table_classifier_utilities.eval_numeric_pattern(pattern)]
                    + first_column_all_patterns_numeric,
                    [len(t) for t in column_patterns],
                )
                column_values = [first_value] + first_column_data_values
                column_tokens = [value_tokens] + first_column_value_tokens

                candidate_tokens = set(
                    [t for t in value_tokens if any(c.isalpha() for c in t)]
                )
                if len(column_values) > 2:
                    candidate_count_of_value = np.count_nonzero(
                        column_values[
                            2 : min(args.max_summary_strength, len(column_values))
                        ]
                        == str(value).strip()
                    )
                else:
                    candidate_count_of_value = 0
                partof_multiword_value_repeats = dict()
                for part in candidate_tokens:
                    partof_multiword_value_repeats[part] = 0
                    for value_tokens in column_tokens:
                        if part in value_tokens:
                            partof_multiword_value_repeats[part] += 1

                consistent_symbol_sets, _ = is_consistent_symbol_sets(column_symbols)
                cand_subhead_data_cell_rules_fired = []
                data_rules_fired[0] = {}
                data_rules_fired[0][0] = {}
                data_rules_fired[0][0]["agreements"] = []
                data_rules_fired[0][0]["null_equivalent"] = False
                for rule in fuzzy_rules["cell"]["data"].keys():
                    rule_fired = False
                    non_empty_patterns = 0
                    if (
                        rule not in ignore_rules["cell"]["data"]
                        and len(column_patterns) > 0
                        and first_value.lower() not in pat_util.null_equivalent_values
                    ):
                        for pattern in column_patterns:
                            if pattern != []:
                                non_empty_patterns += 1
                        if len(column_patterns) >= 2 and non_empty_patterns >= 2:
                            rule_fired = eval_data_cell_rule(
                                rule,
                                column_values,
                                column_tokens,
                                value_pattern_summary,
                                value_chain_consistent,
                                value_pattern_BW_summary,
                                value_symbol_summary,
                                column_symbols,
                                column_patterns,
                                case_summary,
                                candidate_count_of_value,
                                partof_multiword_value_repeats,
                                candidate_tokens,
                                consistent_symbol_sets,
                                all_patterns_numeric,
                                len(column_values),
                            )
                            if rule_fired == True and "_REPEATS_" not in rule:
                                data_rules_fired[0][0]["agreements"].append(rule)

                value_disagreements = []
                disagreement_summary_strength = summary_strength - 1
                if len(pattern) > 0:
                    repetitions_of_candidate = column_values[1:].count(first_value)
                    neighbor = ""
                    try:
                        neighbor = column_values[1]
                        repetitions_of_neighbor = column_values[2:].count(neighbor)
                    except:
                        repetitions_of_neighbor = 0

                    for rule in fuzzy_rules["cell"]["not_data"].keys():
                        rule_fired = False
                        if (
                            rule not in ignore_rules["cell"]["not_data"]
                            and disagreement_summary_strength > 0
                            and (
                                all_numbers(column_symbols) == False
                                or is_number(symbols) == False
                            )
                        ):
                            rule_fired = eval_not_data_cell_rule(
                                rule,
                                repetitions_of_candidate,
                                repetitions_of_neighbor,
                                neighbor,
                                value_pattern_summary,
                                value_pattern_BW_summary,
                                value_chain_consistent,
                                value_symbol_summary,
                                case_summary,
                                length_summary,
                                pattern,
                                symbols,
                                case,
                                value_num_chars,
                                disagreement_summary_strength,
                                data_rules_fired,
                                0,
                                0,
                            )
                            if rule_fired == True and "_REPEATS_" not in rule:
                                value_disagreements.append(rule)

                #######################################################################v######
                #  DATA value classification
                data_score = max_score(
                    data_rules_fired[0][0]["agreements"],
                    fuzzy_rules["cell"]["data"],
                    args.weight_lower_bound,
                )
                POPULATION_WEIGHT = 1 - (1 - args.p) ** (2 * summary_strength)
                if data_score != None:
                    if args.summary_population_factor:
                        cell_data_score = data_score * POPULATION_WEIGHT
                    else:
                        cell_data_score = data_score

                #######################################################################v######
                #  NOT DATA value classification
                not_data_score = max_score(
                    value_disagreements,
                    fuzzy_rules["cell"]["not_data"],
                    args.not_data_weight_lower_bound,
                )
                POPULATION_WEIGHT = 1 - (1 - args.p) ** (
                    2 * disagreement_summary_strength
                )
                if not_data_score != None:
                    if args.summary_population_factor:
                        cell_not_data_score = not_data_score * POPULATION_WEIGHT
                    else:
                        cell_not_data_score = not_data_score

                if (
                    cell_data_score > cell_not_data_score
                ):  # candidate subheader is definitely data, move along
                    continue

                if (
                    row.Index - 1 in predicted_pat_sub_headers
                    and row.Index - 2 in predicted_pat_sub_headers
                ):
                    continue

                if row.Index != cand_data.index[-1]:
                    predicted_pat_sub_headers.append(row.Index)

    # print(f'predicted_pat_sub_headers={predicted_pat_sub_headers}')
    for s_i, subheader in enumerate(predicted_pat_sub_headers):
        if subheader not in subheader_scope.keys():
            if s_i + 1 == len(predicted_pat_sub_headers):
                subheader_scope[subheader] = list(
                    range(subheader + 1, cand_data.index[-1] + 1)
                )
            else:
                next_s_i = s_i + 1
                while next_s_i < len(predicted_pat_sub_headers):
                    next_subh = predicted_pat_sub_headers[next_s_i]
                    if next_subh not in subheader_scope:
                        subheader_scope[subheader] = list(
                            range(subheader + 1, next_subh)
                        )
                        break
                    next_s_i += 1

    return aggregation_rows, subheader_scope


def predict_last_data_line_top_down(
    dataframe,
    predicted_fdl,
    data_confidence,
    not_data_confidence,
    model,
    subheader_scope,
    aggregation_rows,
    blank_lines,
    headers_discovered,
    signatures,
    downwards_data_rules_fired,
    downwards_not_data_rules_fired,
):
    args = model.parameters
    fuzzy_rules = model.fuzzy_rules

    predicted_pat_sub_headers = list(subheader_scope.keys())
    certain_data = []
    certain_data_widths = []
    data_predictions = dict()

    data_rules_fired = {}
    not_data_rules_fired = {}
    predicted_ldl = predicted_fdl
    predicted_subheaders = []

    data = pd.DataFrame()
    candidate_data = dataframe.loc[predicted_fdl:]
    probation = []

    if args.max_attributes != None:
        max_attributes = args.max_attributes
        if args.ignore_left != None:
            max_attributes = args.max_attributes + args.ignore_left
        slice_idx = min(max_attributes, candidate_data.shape[1]) + 1
        candidate_data = candidate_data.iloc[:, :slice_idx]

    line_counter = 0
    patterns = Patterns()
    for line_label, line in candidate_data.iterrows():
        line_counter += 1
        row_values = [str(elem) if elem is not None else elem for elem in line.tolist()]
        first_value = row_values[0].lower()
        if first_value.startswith('"') and first_value.endswith('"'):
            first_value = first_value[1:-1]

        IS_DATA = False
        FOOTNOTE_FOUND = False
        data_conf = 0
        not_data_conf = 0

        # input()
        # print(f'\n---------------\nLINE {line_label}: IS_DATA init {IS_DATA}')
        # print(f'{row_values}')
        if line_label not in blank_lines:
            if line_label in certain_data:
                IS_DATA = True
                # print(f'(1) IS_DATA = True')
                data_conf = 1

            if line_label in predicted_pat_sub_headers:
                for footnote_keyword in pat_util.footnote_keywords:
                    if first_value.startswith(footnote_keyword):
                        FOOTNOTE_FOUND = True
                        not_data_conf = 1
                        break  # stop looking for footnote keywords
                if len(first_value) > 5 and (
                    (
                        (first_value[0] == "1" or first_value[0] == "a")
                        and first_value[1] in [" ", ".", "/", ")", "]", ":"]
                    )
                    or (
                        first_value[0] == "("
                        and (first_value[1].isdigit() or first_value[1] == "a")
                        and first_value[2] == ")"
                    )
                ):
                    FOOTNOTE_FOUND = True
                    not_data_conf = 1

                if FOOTNOTE_FOUND == False:
                    not_data_conf = 1
                    prediction, _ = predict_line_label(data_conf, not_data_conf)
                    data_predictions[line_label] = prediction
                    continue

            if (
                len(row_values) > 0
                and row_values[0].lower() not in ["", "none", "nan"]
                and (
                    (
                        len(row_values) > 1
                        and len(
                            [
                                i
                                for i in row_values[1:]
                                if i.lower() not in ["", "none", "nan"]
                            ]
                        )
                        == 0
                    )
                    or (
                        len(row_values) > 2
                        and len(
                            [
                                i
                                for i in row_values[2:]
                                if i.lower() not in ["", "none", "nan"]
                            ]
                        )
                        == 0
                    )
                )
            ):
                # IS_DATA = True

                # print(f'(2) IS_DATA = True')
                for footnote_keyword in pat_util.footnote_keywords:
                    if first_value.startswith(footnote_keyword):
                        FOOTNOTE_FOUND = True
                        # print('### footnote found')
                        break

                if "=" in first_value:
                    FOOTNOTE_FOUND = True

                if len(first_value) > 5 and (
                    (first_value[0] == "1" or first_value[0] == "a")
                    and first_value[1] in [" ", ".", "/", ")", "]", ":"]
                ):
                    FOOTNOTE_FOUND = True

                if len(first_value) > 5 and (
                    first_value[0] == "("
                    and (first_value[1] == "1" or first_value[1] == "a")
                    and first_value[2] == ")"
                ):
                    FOOTNOTE_FOUND = True

                if (
                    first_value.startswith("(")
                    and len(row_values) > 1
                    and len([i for i in row_values[1:] if i != "nan"]) == 0
                ):
                    FOOTNOTE_FOUND = True

                if FOOTNOTE_FOUND == True:
                    footnote_start = line_label
                    IS_DATA = False
                    # print(f'FOOTNOTE_FOUND, IS_DATA = False')
                    not_data_conf = 1
                    prediction, _ = predict_line_label(data_conf, not_data_conf)
                    data_predictions[line_label] = prediction
                    break

            # for the first 3 lines rely on the classification from first data line search.
            if (
                line_counter <= 3 or line_label in aggregation_rows
            ):  # and line_predictions[line_label]['label']=='DATA':
                data = pd.DataFrame([line], index=[line_label]).append(data)
                certain_data_widths.append(non_empty_values(line))
                predicted_ldl = line_label
                data_conf = 1
                prediction, _ = predict_line_label(data_conf, not_data_conf)
                data_predictions[line_label] = prediction
                continue
            else:
                if line.isnull().values.all() == True:
                    prediction, _ = predict_line_label(data_conf, not_data_conf)
                    data_predictions[line_label] = prediction
                    # print('line.isnull().values.all()==True, line = {line}')
                    continue

                elif ",".join(line.apply(str).tolist()) in set(
                    headers_discovered.values()
                ):
                    IS_DATA = False
                    # print(f'headers_discovered, IS_DATA = False')
                else:
                    (
                        data_rules_fired,
                        not_data_rules_fired,
                        patterns,
                    ) = collect_line_rules(
                        line,
                        predicted_fdl,
                        line_label,
                        data,
                        signatures,
                        model,
                        data_rules_fired,
                        not_data_rules_fired,
                        patterns,
                    )
                    candidate_row_agreements = []
                    candidate_row_disagreements = []
                    for column in candidate_data:
                        #################################################v######################v######
                        #  DATA value classification
                        value_agreements = data_rules_fired[line_label][column][
                            "agreements"
                        ]
                        summary_strength = data_rules_fired[line_label][column][
                            "summary_strength"
                        ]

                        data_score = max_score(
                            value_agreements,
                            fuzzy_rules["cell"]["data"],
                            args.weight_lower_bound,
                        )
                        POPULATION_WEIGHT = 1 - (1 - args.p) ** (2 * summary_strength)
                        if data_score != None:
                            if args.summary_population_factor:
                                candidate_row_agreements.append(
                                    data_score * POPULATION_WEIGHT
                                )
                            else:
                                candidate_row_agreements.append(data_score)
                        #######################################################################v######
                        #  NOT DATA value classification
                        value_disagreements = not_data_rules_fired[line_label][column][
                            "disagreements"
                        ]
                        disagreement_summary_strength = not_data_rules_fired[
                            line_label
                        ][column]["disagreement_summary_strength"]
                        not_data_score = max_score(
                            value_disagreements,
                            fuzzy_rules["cell"]["not_data"],
                            args.not_data_weight_lower_bound,
                        )
                        POPULATION_WEIGHT = 1 - (1 - args.p) ** (
                            2 * disagreement_summary_strength
                        )

                        if not_data_score != None:
                            if args.summary_population_factor:
                                candidate_row_disagreements.append(
                                    not_data_score * POPULATION_WEIGHT
                                )
                            else:
                                candidate_row_disagreements.append(not_data_score)
                    #################################################################################
                    # NOT DATA line weights
                    line_not_data_evidence = [
                        score for score in candidate_row_disagreements
                    ]
                    if args.weight_input == "values_and_lines":
                        if candidate_data.shape[1] > 1:
                            not_data_line_rules_fired = downwards_not_data_rules_fired[
                                line_label
                            ]["line"]
                            for event in not_data_line_rules_fired:
                                if event == "UP_TO_FIRST_COLUMN_COMPLETE_CONSISTENTLY":
                                    continue
                                if (
                                    event.startswith("ADJACENT_ARITHMETIC_SEQUENCE")
                                    and fuzzy_rules["line"]["not_data"][event]["weight"]
                                    == None
                                ):
                                    steps = event[-1]
                                    if steps.isdigit() and int(steps) in range(2, 6):
                                        event = event[:-1] + str(int(steps) + 1)
                                if (
                                    event in fuzzy_rules["line"]["not_data"].keys()
                                    and fuzzy_rules["line"]["not_data"][event]["weight"]
                                    != None
                                    and fuzzy_rules["line"]["not_data"][event]["weight"]
                                    > args.not_data_weight_lower_bound
                                ):
                                    line_not_data_evidence.append(
                                        fuzzy_rules["line"]["not_data"][event]["weight"]
                                    )

                    not_data_conf = probabilistic_sum(line_not_data_evidence)

                    # DATA line weights
                    line_is_data_evidence = [
                        score for score in candidate_row_agreements
                    ]
                    if args.weight_input == "values_and_lines":
                        line_is_data_events = downwards_data_rules_fired[line_label][
                            "line"
                        ]
                        for rule in line_is_data_events:
                            if (
                                fuzzy_rules["line"]["data"][rule]["weight"] != None
                                and fuzzy_rules["line"]["data"][rule]["weight"]
                                > args.weight_lower_bound
                            ):
                                line_is_data_evidence.append(
                                    fuzzy_rules["line"]["data"][rule]["weight"]
                                )
                    # calculate confidence that this row is data
                    data_conf = probabilistic_sum(line_is_data_evidence)

                    # print(f'{line_label}: \n\t-data_conf={data_conf}\n\t-not_data_conf={not_data_conf}')
                    if data_conf > 0 and data_conf >= not_data_conf:
                        IS_DATA = True
                        # print(f'(3) IS_DATA = True (data_conf>0 and data_conf>=not_data_conf)')
                    elif (
                        len(certain_data_widths) > 0
                        and non_empty_values(line) == max(certain_data_widths)
                        and line_label - 1 in data.index
                    ):  # TODO refactor as rule
                        IS_DATA = True
                        # print(f'(4) IS_DATA = True')
                    else:
                        prediction, _ = predict_line_label(
                            data_confidence[line_label], not_data_confidence[line_label]
                        )
                        if (
                            line_label - 1 not in probation
                            and line_label - 1 not in blank_lines
                            and data_conf < not_data_conf
                            and prediction["label"] != "DATA"
                        ):
                            probation.append(line_label)
                        elif (data_conf > 0 and data_conf >= not_data_conf) or (
                            line_label - 1 not in probation
                            and prediction["label"] == "DATA"
                        ):
                            IS_DATA = True
                            # print(f'(5) IS_DATA = True')

            # print(f'\t{line_label} IS_DATA={IS_DATA}\n')

            # --- end if blanklines
            if line_label in probation:
                prediction, _ = predict_line_label(data_conf, not_data_conf)
                data_predictions[line_label] = prediction
                continue

            elif IS_DATA == True:
                predicted_ldl = line_label
                data = pd.DataFrame([line], index=[line_label]).append(data)
                certain_data_widths.append(non_empty_values(line))

            else:
                break
        else:
            not_data_conf = 1
            break

        prediction, _ = predict_line_label(data_conf, not_data_conf)
        data_predictions[line_label] = prediction

    # pp.pprint(data_predictions)
    bottom_boundary_confidence = table_classifier_utilities.last_data_line_confidence(
        data_predictions, predicted_ldl
    )
    return predicted_ldl, bottom_boundary_confidence


class Patterns:
    def __init__(self):
        self.data = dict()
        self.not_data = dict()
        self.summary = dict()

    def data_initialize(
        self,
        column_index,
        value,
        candidate_tokens,
        column_values,
        column_tokens,
        column_trains,
        column_bw_trains,
        column_symbols,
        column_cases,
        column_char_lengths,
        column_is_numeric_train,
        max_values_lookahead,
    ):

        self.data[column_index] = dict()
        self.data[column_index]["train"] = pat_util.generate_pattern_summary(
            column_trains
        )
        self.data[column_index]["bw_train"] = pat_util.generate_pattern_summary(
            column_bw_trains
        )
        self.data[column_index]["symbolset"] = pat_util.generate_symbol_summary(
            column_symbols
        )
        self.data[column_index]["case"] = pat_util.generate_case_summary(column_cases)
        self.data[column_index]["character_length"] = pat_util.generate_length_summary(
            column_char_lengths
        )
        self.data[column_index]["summary_strength"] = sum(
            1 for x in column_trains if len(x) > 0
        )
        self.data[column_index]["candidate_count"] = dict()
        self.data[column_index]["candidate_count"][value] = np.count_nonzero(
            column_values[2 : min(max_values_lookahead, len(column_values))] == value
        )
        self.data[column_index]["consistent_symbol_sets"] = is_consistent_symbol_sets(
            column_symbols
        )
        self.data[column_index][
            "column_is_numeric"
        ] = pat_util.generate_all_numeric_sig_pattern(
            column_is_numeric_train, [len(t) for t in column_trains]
        )
        self.data[column_index][
            "partof_multiword_value_repeats"
        ] = dict()  # TODO check if this contributes to quadratic? ^o^
        for part in candidate_tokens:
            self.data[column_index]["partof_multiword_value_repeats"][part] = 0
            for value_tokens in column_tokens:
                if part in value_tokens:
                    self.data[column_index]["partof_multiword_value_repeats"][part] += 1

    def generate_column_patterns(self, column_series, outlier_sensitive=True):
        signatures = TableSignatures(column_series, outlier_sensitive)
        self.summary["train"] = pat_util.generate_pattern_summary(
            signatures.all_column_train
        )
        self.summary["bw_train"] = pat_util.generate_pattern_summary(
            signatures.all_column_bw_train
        )
        self.summary["symbolset"] = pat_util.generate_symbol_summary(
            signatures.all_column_symbols
        )
        self.summary["case"] = pat_util.generate_case_summary(
            signatures.all_column_cases
        )
        self.summary["character_length"] = pat_util.generate_length_summary(
            signatures.all_column_character_lengths
        )

    def data_increment(
        self,
        column_index,
        value,
        train_sig,
        bw_train_sig,
        symbol_sig,
        case,
        num_chars_sig,
        numeric_train_sig,
        candidate_tokens,
    ):

        self.data[column_index]["train"] = pat_util.train_incremental_pattern(
            self.data[column_index]["train"], train_sig
        )
        self.data[column_index]["bw_train"] = pat_util.train_incremental_pattern(
            self.data[column_index]["bw_train"], bw_train_sig
        )
        self.data[column_index]["symbolset"] = pat_util.symbolset_incremental_pattern(
            self.data[column_index]["symbolset"], symbol_sig
        )
        self.data[column_index]["case"] = pat_util.case_incremental_pattern(
            self.data[column_index]["case"], case
        )
        self.data[column_index][
            "character_length"
        ] = pat_util.charlength_incremental_pattern(
            self.data[column_index]["character_length"], num_chars_sig
        )
        self.data[column_index][
            "summary_strength"
        ] = pat_util.summary_strength_increment(
            self.data[column_index]["summary_strength"], train_sig
        )
        self.data[column_index]["candidate_count"] = pat_util.candidate_count_increment(
            self.data[column_index]["candidate_count"], value
        )
        self.data[column_index][
            "partof_multiword_value_repeats"
        ] = pat_util.token_repeats_increment(
            self.data[column_index]["partof_multiword_value_repeats"], candidate_tokens
        )
        self.data[column_index][
            "consistent_symbol_sets"
        ] = pat_util.consistent_symbol_sets_increment(
            self.data[column_index]["consistent_symbol_sets"], symbol_sig
        )
        self.data[column_index][
            "column_is_numeric"
        ] = pat_util.numeric_train_incremental_pattern(
            numeric_train_sig,
            len(train_sig),
            self.data[column_index]["column_is_numeric"],
        )

    def not_data_initialize(
        self,
        column_index,
        column_trains,
        column_bw_trains,
        column_symbols,
        column_cases,
        column_char_lengths,
        column_isnumber,
        train_sig,
        signatures_slice,
    ):
        self.not_data[column_index] = dict()
        self.not_data[column_index]["train"] = pat_util.generate_pattern_summary(
            column_trains
        )
        self.not_data[column_index]["bw_train"] = pat_util.generate_pattern_summary(
            column_bw_trains
        )
        self.not_data[column_index]["symbolset"] = pat_util.generate_symbol_summary(
            column_symbols
        )
        self.not_data[column_index]["case"] = pat_util.generate_case_summary(
            column_cases
        )
        self.not_data[column_index][
            "character_length"
        ] = pat_util.generate_length_summary(column_char_lengths)
        self.not_data[column_index]["all_numbers"] = np.all(column_isnumber)
        self.not_data[column_index]["disagreement_summary_strength"] = sum(
            1 for x in column_trains if len(x) > 0
        )

        self.not_data[column_index]["candidate_count"] = dict()
        self.not_data[column_index]["neighbor_count"] = dict()

        if len(train_sig) > 0:
            value = signatures_slice.all_normalized_values[0, column_index]
            self.not_data[column_index]["candidate_count"][value] = 0
            context_values = signatures_slice.all_normalized_values[1:, column_index]
            self.not_data[column_index]["candidate_count"][value] = np.count_nonzero(
                context_values[1:] == value
            )
            neighbor = ""
            try:
                neighbor = context_values[1]
                self.not_data[column_index]["neighbor_count"][
                    neighbor
                ] = np.count_nonzero(context_values[2:] == neighbor)
            except:
                self.not_data[column_index]["neighbor_count"][neighbor] = 0

    def not_data_increment(self, column_index, signatures_slice):

        last_train = signatures_slice.train_normalized_numbers[1, column_index]
        last_bw_train_sig = signatures_slice.all_column_bw_train[1, column_index]
        last_symbol_sig = signatures_slice.all_column_symbols[1, column_index]
        last_case = signatures_slice.all_column_cases[1, column_index]
        # last_num_tokens_sig = signatures_slice.all_column_token_lengths[1, column_index]
        last_num_chars_sig = signatures_slice.all_column_character_lengths[
            1, column_index
        ]
        last_is_number = signatures_slice.all_column_isnumber[1, column_index]

        self.not_data[column_index]["train"] = pat_util.train_incremental_pattern(
            self.not_data[column_index]["train"], last_train
        )
        self.not_data[column_index]["bw_train"] = pat_util.train_incremental_pattern(
            self.not_data[column_index]["bw_train"], last_bw_train_sig
        )
        self.not_data[column_index][
            "symbolset"
        ] = pat_util.symbolset_incremental_pattern(
            self.not_data[column_index]["symbolset"], last_symbol_sig
        )
        self.not_data[column_index]["case"] = pat_util.case_incremental_pattern(
            self.not_data[column_index]["case"], last_case
        )
        self.not_data[column_index][
            "character_length"
        ] = pat_util.charlength_incremental_pattern(
            self.not_data[column_index]["character_length"], last_num_chars_sig
        )

        if len(last_symbol_sig) > 0:
            self.not_data[column_index]["all_numbers"] = np.all(
                [self.not_data[column_index]["all_numbers"], last_is_number]
            )

        if len(last_train) > 0:
            self.not_data[column_index]["disagreement_summary_strength"] + 1

        train_sig = signatures_slice.train_normalized_numbers[0, column_index]
        value = signatures_slice.all_normalized_values[0, column_index]

        if len(train_sig) > 0:
            context_values = signatures_slice.all_normalized_values[1:, column_index]
            self.not_data[column_index][
                "candidate_count"
            ] = pat_util.candidate_count_increment(
                self.not_data[column_index]["candidate_count"], value
            )
            neighbor = ""
            try:
                neighbor = context_values[1]
                self.not_data[column_index][
                    "neighbor_count"
                ] = pat_util.candidate_count_increment(
                    self.not_data[column_index]["neighbor_count"], neighbor
                )
            except:
                self.not_data[column_index]["neighbor_count"] = 0


def collect_line_rules(
    line,
    predicted_fdl,
    line_label,
    data,
    signatures,
    model,
    line_agreements,
    line_disagreements,
    patterns,
):
    # print(signatures.preview())
    args = model.parameters
    ignore_rules = model.ignore_rules
    signatures_slice = signatures.reverse_slice(top=predicted_fdl, bottom=line_label)

    # row_values = [str(elem) if elem is not None else elem for elem in line.values]
    # null_equivalent_fired, times = line_has_null_equivalent(row_values)
    null_equivalent_fired = False
    times = sum(
        signatures.is_null_equivalent[line_label, :]
    )  # this wont work, it has all null equivalent, we care about strictly nulls
    if times > 0:
        null_equivalent_fired = True

    all_summaries_empty = True
    max_values_lookahead = data.shape[0]
    coherent_cells = dict()
    incoherent_cells = dict()
    for column_index, column in enumerate(line.index):
        coherent_cells[column] = {}
        incoherent_cells[column] = {}
        coherent_cells[column]["agreements"] = []
        incoherent_cells[column]["disagreements"] = []
        value = signatures.all_normalized_values[line_label, column_index]
        value_lower = value.lower()
        value_tokens = signatures.all_column_tokens[line_label, column_index]
        is_aggregate = signatures.is_aggregate[
            line_label, column_index
        ]  # (len(value_tokens)>0 and not set(value_tokens).isdisjoint(pat_util.aggregation_tokens))
        is_null_equivalent = signatures.is_null_equivalent[
            line_label, column_index
        ]  # (value_lower in pat_util.null_equivalent_values)

        train_sig = signatures.all_column_train[line_label, column_index]
        bw_train_sig = signatures.all_column_bw_train[line_label, column_index]
        symbol_sig = signatures.all_column_symbols[line_label, column_index]
        case = signatures.all_column_cases[line_label, column_index]
        num_tokens_sig = signatures.all_column_token_lengths[line_label, column_index]
        num_chars_sig = signatures.all_column_character_lengths[
            line_label, column_index
        ]
        numeric_train_sig = signatures.all_column_is_numeric_train[
            line_label, column_index
        ]
        is_number = signatures.all_column_isnumber[line_label, column_index]
        coherent_cells[column]["null_equivalent"] = is_null_equivalent
        coherent_cells[column]["aggregate"] = is_aggregate

        column_values = signatures_slice.all_normalized_values[:, column_index]
        column_tokens = signatures_slice.all_column_tokens[:, column_index]
        candidate_tokens = set([t for t in value_tokens if any(c.isalpha() for c in t)])
        column_trains = signatures_slice.train_normalized_numbers[:, column_index]
        column_symbols = signatures_slice.symbolset_normalized_numbers[:, column_index]
        column_bw_trains = signatures_slice.all_column_bw_train[:, column_index]
        column_cases = signatures_slice.all_column_cases[:, column_index]
        column_char_lengths = signatures_slice.all_column_character_lengths[
            :, column_index
        ]
        column_is_numeric_train = signatures_slice.all_column_is_numeric_train[
            :, column_index
        ]

        if column_index not in patterns.data.keys():
            patterns.data_initialize(
                column_index,
                value,
                candidate_tokens,
                column_values,
                column_tokens,
                column_trains,
                column_bw_trains,
                column_symbols,
                column_cases,
                column_char_lengths,
                column_is_numeric_train,
                max_values_lookahead,
            )
        else:
            patterns.data_increment(
                column_index,
                value,
                train_sig,
                bw_train_sig,
                symbol_sig,
                case,
                num_chars_sig,
                numeric_train_sig,
                candidate_tokens,
            )

        # patterns of a window INCLUDING the cell we are on
        data_patterns = patterns.data[column_index]
        value_pattern_summary, value_chain_consistent = data_patterns["train"]
        value_pattern_BW_summary, _ = data_patterns["bw_train"]
        value_symbol_summary = data_patterns["symbolset"]
        case_summary = data_patterns["case"]
        length_summary = data_patterns["character_length"]
        summary_strength = data_patterns["summary_strength"]
        candidate_count_of_value = data_patterns["candidate_count"][value]
        partof_multiword_value_repeats = data_patterns["partof_multiword_value_repeats"]
        consistent_symbol_sets, _ = data_patterns["consistent_symbol_sets"]
        train_sigs_all_numeric, _ = data_patterns["column_is_numeric"]

        coherent_cells[column]["summary_strength"] = summary_strength

        if (
            null_equivalent_fired == True
            or len(value_pattern_summary) > 0
            or len(value_pattern_BW_summary) > 0
            or len(value_symbol_summary) > 0
            or len(case_summary) > 0
        ):
            all_summaries_empty = False

        for rule in model.fuzzy_rules["cell"]["data"].keys():
            rule_fired = False
            # Don't bother looking for coherency if there are no patterns or if the value on this line gives an empty pattern
            # non_empty_patterns=0
            if (
                rule not in ignore_rules["cell"]["data"]
                and len(column_trains) > 0
                and is_null_equivalent == False
            ):
                # there is no point calculating agreement over one value, a single value always agrees with itself.
                # in addition, many tables have bilingual headers, so agreement between two header values is very common, require nij>=3
                if len(column_trains) >= 2 and summary_strength >= 2:
                    rule_fired = eval_data_cell_rule(
                        rule,
                        column_values,
                        column_tokens,
                        value_pattern_summary,
                        value_chain_consistent,
                        value_pattern_BW_summary,
                        value_symbol_summary,
                        column_symbols,
                        column_trains,
                        case_summary,
                        candidate_count_of_value,
                        partof_multiword_value_repeats,
                        candidate_tokens,
                        consistent_symbol_sets,
                        train_sigs_all_numeric,
                        max_values_lookahead,
                    )

            if rule_fired == True:
                coherent_cells[column]["agreements"].append(rule)

        ############################################ NOT DATA #####################################
        column_values = signatures_slice.all_normalized_values[1:, column_index]
        column_trains = signatures_slice.train_normalized_numbers[1:, column_index]
        column_bw_trains = signatures_slice.all_column_bw_train[1:, column_index]
        column_symbols = signatures_slice.symbolset_normalized_numbers[1:, column_index]
        column_cases = signatures_slice.all_column_cases[1:, column_index]
        column_char_lengths = signatures_slice.all_column_character_lengths[
            1:, column_index
        ]
        column_isnumber = signatures_slice.all_column_isnumber[1:, column_index]

        new_value = value
        if "D" in symbol_sig and symbol_sig.issubset(
            set(["D", ".", ",", "S", "-", "+", "~", "(", ")"])
        ):
            # ## Replace above with this:
            train_sig = signatures.train_normalized_numbers[line_label, column_index]
            symbol_sig = signatures.symbolset_normalized_numbers[
                line_label, column_index
            ]
            case = signatures.all_column_cases[line_label, column_index]
            num_chars_sig = signatures.all_column_character_lengths[
                line_label, column_index
            ]
            ###############################################################################################
        if column_index not in patterns.not_data.keys():
            # initialize patterns
            patterns.not_data_initialize(
                column_index,
                column_trains,
                column_bw_trains,
                column_symbols,
                column_cases,
                column_char_lengths,
                column_isnumber,
                train_sig,
                signatures_slice,
            )
        else:
            # increment patterns
            patterns.not_data_increment(column_index, signatures_slice)

        # patterns of a window that does NOT contain the cell we are on
        not_data_patterns = patterns.not_data[column_index]
        value_pattern_summary, value_chain_consistent = not_data_patterns["train"]
        value_pattern_BW_summary, _ = not_data_patterns["bw_train"]
        value_symbol_summary = not_data_patterns["symbolset"]
        case_summary = not_data_patterns["case"]
        length_summary = not_data_patterns["character_length"]
        all_numbers_summary = not_data_patterns["all_numbers"]
        disagreement_summary_strength = not_data_patterns[
            "disagreement_summary_strength"
        ]
        ## REFACTORED
        repetitions_of_candidate = 0
        repetitions_of_neighbor = 0
        neighbor = ""
        if len(train_sig) > 0:
            repetitions_of_candidate = not_data_patterns["candidate_count"][value]
            if signatures_slice.all_normalized_values.shape[0] > 2:
                neighbor = signatures_slice.all_normalized_values[2, column]
                if neighbor != "":
                    repetitions_of_neighbor = not_data_patterns["neighbor_count"][
                        neighbor
                    ]

        incoherent_cells[column][
            "disagreement_summary_strength"
        ] = disagreement_summary_strength

        for rule in model.fuzzy_rules["cell"]["not_data"].keys():
            rule_fired = False
            if rule not in ignore_rules["cell"]["not_data"] and len(train_sig) > 0:
                if disagreement_summary_strength > 0 and (
                    all_numbers_summary == False or is_number == False
                ):
                    rule_fired = eval_not_data_cell_rule(
                        rule,
                        repetitions_of_candidate,
                        repetitions_of_neighbor,
                        neighbor,
                        value_pattern_summary,
                        value_pattern_BW_summary,
                        value_chain_consistent,
                        value_symbol_summary,
                        case_summary,
                        length_summary,
                        train_sig,
                        symbol_sig,
                        case,
                        num_chars_sig,
                        disagreement_summary_strength,
                        line_agreements,
                        column,
                        line_label,
                    )
                    if rule_fired == True:
                        incoherent_cells[column]["disagreements"].append(rule)

    # Collect data line rules fired
    coherent_cells["all_summaries_empty"] = all_summaries_empty
    line_agreements[line_label] = coherent_cells
    line_disagreements[line_label] = incoherent_cells

    return line_agreements, line_disagreements, patterns


def non_empty_values(df_row):
    last_idx = df_row.last_valid_index()
    return df_row.loc[:last_idx].shape[0]


def pythonify(json_data):

    correctedDict = {}

    for key, value in json_data.items():
        if isinstance(value, list):
            value = [
                pythonify(item) if isinstance(item, dict) else item for item in value
            ]
        elif isinstance(value, dict):
            value = pythonify(value)
        try:
            key = int(key)
        except Exception as ex:
            pass
        correctedDict[key] = value

    return correctedDict


def get_class_confidences(
    file_dataframe_trimmed,
    data_rules_fired,
    not_data_rules_fired,
    fuzzy_rules,
    parameters,
):
    # print('\n\n---> [METHOD] pat.get_class_confidences>>\n')
    data_line_confidences = dict()
    not_data_line_confidences = dict()
    label_confidences = dict()

    column_indexes = file_dataframe_trimmed.columns
    # print(f'column_indexes={column_indexes}')
    before_data = True
    offset = file_dataframe_trimmed.index[0]
    for row_index in file_dataframe_trimmed.index:

        # if offset+parameters.max_candidates<row_index:
        #     break

        label_confidences[row_index] = dict()
        candidate_row_agreements = list()
        candidate_row_disagreements = list()

        for column_index in column_indexes:
            #############################################################################
            #  DATA value classification
            value_agreements = data_rules_fired[row_index][column_index]["agreements"]
            summary_strength = data_rules_fired[row_index][column_index][
                "summary_strength"
            ]

            # if there are no lines below me to check agreement,
            # and line before me exists and was data
            # see impute agreements

            if (
                (
                    row_index in data_rules_fired.keys()
                    and data_rules_fired[row_index][column_index]["null_equivalent"]
                    == True
                    or data_rules_fired[row_index][column_index]["summary_strength"]
                    == 1
                )
                and parameters.impute_nulls == True
                and row_index - 1 in data_rules_fired.keys()
                and column_index in data_rules_fired[row_index - 1].keys()
                and row_index - 1 in data_line_confidences.keys()
                and data_line_confidences[row_index - 1]
                > not_data_line_confidences[row_index - 1]
            ):
                value_agreements = data_rules_fired[row_index - 1][column_index][
                    "agreements"
                ]
                summary_strength = data_rules_fired[row_index - 1][column_index][
                    "summary_strength"
                ]
            if (
                row_index in data_rules_fired.keys()
                and data_rules_fired[row_index][column_index]["summary_strength"] == 0
                and data_rules_fired[row_index][column_index]["aggregate"]
                and row_index - 2 in data_rules_fired.keys()
                and column_index in data_rules_fired[row_index - 2].keys()
                and row_index - 2 in data_line_confidences.keys()
                and data_line_confidences[row_index - 2]
                > not_data_line_confidences[row_index - 2]
            ):
                value_agreements = data_rules_fired[row_index - 2][column_index][
                    "agreements"
                ]
                summary_strength = data_rules_fired[row_index - 2][column_index][
                    "summary_strength"
                ]

            # otherwise, nothing was wrong, i can use my own damn agreements as initialized
            data_score = max_score(
                value_agreements,
                fuzzy_rules["cell"]["data"],
                parameters.weight_lower_bound,
            )
            POPULATION_WEIGHT = 1 - (1 - parameters.p) ** (2 * summary_strength)
            if data_score != None:
                if parameters.summary_population_factor:
                    candidate_row_agreements.append(data_score * POPULATION_WEIGHT)
                else:
                    candidate_row_agreements.append(data_score)

            #######################################################################v######
            #  NOT DATA value classification
            value_disagreements = not_data_rules_fired[row_index][column_index][
                "disagreements"
            ]
            disagreement_summary_strength = not_data_rules_fired[row_index][
                column_index
            ]["disagreement_summary_strength"]
            not_data_score = max_score(
                value_disagreements,
                fuzzy_rules["cell"]["not_data"],
                parameters.not_data_weight_lower_bound,
            )
            POPULATION_WEIGHT = 1 - (1 - parameters.p) ** (
                2 * disagreement_summary_strength
            )

            if not_data_score != None:
                if parameters.summary_population_factor:
                    candidate_row_disagreements.append(
                        not_data_score * POPULATION_WEIGHT
                    )
                else:
                    candidate_row_disagreements.append(not_data_score)

            ########################################################################

        #################################################################################
        # NOT DATA line weights
        line_not_data_evidence = [score for score in candidate_row_disagreements]
        if parameters.weight_input == "values_and_lines":
            if (
                row_index - 1 in data_line_confidences.keys()
                and data_line_confidences[row_index - 1]
                > not_data_line_confidences[row_index - 1]
            ):
                before_data = False
            if file_dataframe_trimmed.shape[1] > 1:
                not_data_line_rules_fired = not_data_rules_fired[row_index]["line"]
                for event in not_data_line_rules_fired:
                    if (
                        event == "UP_TO_FIRST_COLUMN_COMPLETE_CONSISTENTLY"
                        and before_data == False
                    ):
                        continue
                    if (
                        event.startswith("ADJACENT_ARITHMETIC_SEQUENCE")
                        and fuzzy_rules["line"]["not_data"][event]["weight"] == None
                    ):
                        steps = event[-1]
                        if steps.isdigit() and int(steps) in range(2, 6):
                            event = event[:-1] + str(int(steps) + 1)
                    if (
                        event in fuzzy_rules["line"]["not_data"].keys()
                        and fuzzy_rules["line"]["not_data"][event]["weight"] != None
                        and fuzzy_rules["line"]["not_data"][event]["weight"]
                        > parameters.not_data_weight_lower_bound
                    ):
                        line_not_data_evidence.append(
                            fuzzy_rules["line"]["not_data"][event]["weight"]
                        )

        # DATA line weights
        line_is_data_evidence = [score for score in candidate_row_agreements]
        if parameters.weight_input == "values_and_lines":
            line_is_data_events = data_rules_fired[row_index]["line"]
            for rule in line_is_data_events:
                if (
                    fuzzy_rules["line"]["data"][rule]["weight"] != None
                    and fuzzy_rules["line"]["data"][rule]["weight"]
                    > parameters.weight_lower_bound
                ):
                    line_is_data_evidence.append(
                        fuzzy_rules["line"]["data"][rule]["weight"]
                    )

        # calculate confidence that this row is data
        data_conf = probabilistic_sum(line_is_data_evidence)
        data_line_confidences[row_index] = data_conf

        # calculate confidence that this row is not data
        not_data_conf = probabilistic_sum(line_not_data_evidence)
        not_data_line_confidences[row_index] = not_data_conf

        label_confidences[row_index]["DATA"] = data_conf
        label_confidences[row_index]["NOT-DATA"] = not_data_conf

    return data_line_confidences, not_data_line_confidences


def max_score(events, unit_class_fuzzy_rules, weight_lower_bound):
    if len(events) > 0:
        event_score = []
        for event in events:
            if (
                unit_class_fuzzy_rules[event]["weight"] != None
                and unit_class_fuzzy_rules[event]["weight"] >= weight_lower_bound
            ):
                event_score.append((event, unit_class_fuzzy_rules[event]["weight"]))
        if len(event_score) > 0:
            event_score.sort(key=lambda x: x[1], reverse=True)
            return event_score[0][1]
        else:
            return 0
    else:
        return 0


# ALSO IN table_classifier_utilities, # TODO remove from there SAFELY
def probabilistic_sum(line_scores):
    # product_form, demorgan, etc
    predata_row_confidence = 0
    if len(line_scores) > 0:
        score_counts = {x: line_scores.count(x) for x in line_scores}
        prod_list = []
        for score, count in score_counts.items():
            prod_list.append((1 - score) ** count)
        predata_row_confidence = 1 - np.prod(prod_list)
    return predata_row_confidence


def process_csv_worker(task):
    (
        db_cred,
        file_counter,
        file_object,
        pytheas_model,
        endpoint_dbname,
        top_level_dir,
        max_lines,
        fold_id,
    ) = task
    crawl_datafile_key = file_object[0]
    annotations = file_object[1]
    filepath = file_object[2]
    failure = file_object[3]
    total_rows = None
    total_columns = None
    if max_lines != None:
        file_dataframe = file_utilities.get_dataframe(filepath, max_lines)
    else:
        (
            all_csv_tuples,
            discovered_delimiter,
            discovered_encoding,
            encoding_language,
            encoding_confidence,
            failure,
            blanklines,
            google_detected_lang,
        ) = file_utilities.sample_file(filepath, 10)
        num_lines = 0

        all_csv_tuples = []
        if failure == None:
            try:
                with codecs.open(filepath, "rU", encoding=discovered_encoding) as f:
                    chunk = f.read()
                    if chunk:
                        for line in csv.reader(
                            chunk.split("\n"),
                            quotechar='"',
                            delimiter=discovered_delimiter,
                            skipinitialspace=True,
                        ):
                            num_lines += 1
                            if len(line) == 0 or sum(len(s.strip()) for s in line) == 0:
                                blanklines.append(num_lines - 1)
                            all_csv_tuples.append(line)
                file_dataframe = file_utilities.merged_df(failure, all_csv_tuples)
                total_rows, total_columns = file_dataframe.shape
            except Exception as e:
                print(e)
                print(f"discovered_delimiter=<{discovered_delimiter}>")
                failure = str(e)

    bottom_boundary = file_dataframe.shape[0] - 1  # initialize

    if "tables" in annotations.keys():
        annotated_tables = annotations["tables"]

    blank_lines = []
    blank_lines = list(file_dataframe[file_dataframe.isnull().all(axis=1)].index)

    file_dataframe_trimmed = file_dataframe.copy()
    if pytheas_model.parameters.max_attributes != None:
        max_attributes = pytheas_model.parameters.max_attributes
        if pytheas_model.parameters.ignore_left != None:
            max_attributes = (
                pytheas_model.parameters.max_attributes
                + pytheas_model.parameters.ignore_left
            )
        slice_idx = min(max_attributes, file_dataframe.shape[1]) + 1
        file_dataframe_trimmed = file_dataframe.iloc[:, :slice_idx]
    try:
        discovered_tables = pytheas_model.extract_tables(
            file_dataframe_trimmed, blank_lines
        )

        line_predictions, cell_predictions = evaluation_utilities.assign_class(
            file_dataframe_trimmed,
            discovered_tables,
            blank_lines,
            crawl_datafile_key,
            annotations,
            fold_id,
        )

        (
            table_confusion_matrix,
            table_confidences,
        ) = evaluation_utilities.evaluate_relation_extraction(
            annotated_tables, discovered_tables
        )
        table_confidences["crawl_datafile_key"] = crawl_datafile_key

        file_parsed_correctly = False
        if line_predictions["annotated_label"].equals(
            line_predictions["predicted_label"]
        ):
            file_parsed_correctly = True

        return (
            cell_predictions,
            line_predictions,
            file_parsed_correctly,
            table_confusion_matrix,
            table_confidences,
            total_rows,
            total_columns,
            discovered_tables,
        )
    except Exception as e:
        print(
            f"crawl_datafile_key={crawl_datafile_key} failed to process, {e}: {traceback.format_exc()}"
        )


def collect_dataframe_rules(csv_file, model, signatures):

    args = model.parameters
    fuzzy_rules = model.fuzzy_rules
    ignore_rules = model.ignore_rules

    dataframe_labels = []
    for column in csv_file:
        dataframe_labels.append(column)

    data_rules_fired = {}
    not_data_rules_fired = {}
    row_counter = -1

    for row in csv_file.itertuples():
        line_index = row.Index
        if len(row) > 1:
            row = row[1:]
        else:
            row = []

        row_values = [str(elem) if elem is not None else elem for elem in row]
        null_equivalent_fired, times = line_has_null_equivalent(row_values)

        data_rules_fired[line_index] = {}
        row_counter += 1
        all_summaries_empty = True  # initialize

        n_lines = len(signatures.all_normalized_values)
        patterns = Patterns()
        for column_index, column in enumerate(csv_file.columns):

            data_rules_fired[line_index][column_index] = {}
            data_rules_fired[line_index][column_index]["agreements"] = []

            candidate_value = signatures.all_normalized_values[line_index, column_index]
            value_lower = candidate_value.lower()
            value_tokens = value_lower.split()

            is_aggregate = len(value_tokens) > 0 and not set(value_tokens).isdisjoint(
                pat_util.aggregation_tokens
            )
            is_null_equivalent = (
                candidate_value.strip().lower() in pat_util.null_equivalent_values
            )

            data_rules_fired[line_index][column_index][
                "null_equivalent"
            ] = is_null_equivalent
            data_rules_fired[line_index][column_index]["aggregate"] = is_aggregate

            column_train_sigs = None
            column_symbols = None
            column_cases = None
            column_lengths = None
            column_tokens = None

            # we need a context window with up to args.max_summary_strength non empty values to generate a context pattern
            if args.max_summary_strength != None:
                nonempty_patterns = 0
                nonempty_patterns_idx = 0
                for nonempty_patterns_idx in range(
                    0, min(n_lines - line_index, args.max_line_depth)
                ):
                    if (
                        len(
                            signatures.all_column_train[
                                line_index + nonempty_patterns_idx, column_index
                            ]
                        )
                        > 0
                    ):
                        nonempty_patterns += 1
                        if nonempty_patterns == args.max_summary_strength:
                            column_train_sigs = signatures.all_column_train[
                                line_index : line_index + nonempty_patterns_idx + 1,
                                column_index,
                            ].tolist()
                            column_bw_train_sigs = signatures.all_column_bw_train[
                                line_index : line_index + nonempty_patterns_idx + 1,
                                column_index,
                            ].tolist()
                            column_symbols = signatures.all_column_symbols[
                                line_index : line_index + nonempty_patterns_idx + 1,
                                column_index,
                            ]
                            column_cases = signatures.all_column_cases[
                                line_index : line_index + nonempty_patterns_idx + 1,
                                column_index,
                            ]
                            column_lengths = signatures.all_column_character_lengths[
                                line_index : line_index + nonempty_patterns_idx + 1,
                                column_index,
                            ]
                            column_tokens = signatures.all_column_tokens[
                                line_index : line_index + nonempty_patterns_idx + 1,
                                column_index,
                            ]
                            column_values = signatures.all_normalized_values[
                                line_index : line_index + nonempty_patterns_idx + 1,
                                column_index,
                            ]
                            column_is_numeric_train = (
                                signatures.all_column_is_numeric_train[
                                    line_index : line_index + nonempty_patterns_idx + 1,
                                    column_index,
                                ]
                            )
                            break

            if column_train_sigs == None:
                column_train_sigs = signatures.all_column_train[
                    line_index:, column_index
                ].tolist()
                column_bw_train_sigs = signatures.all_column_bw_train[
                    line_index:, column_index
                ].tolist()
                column_symbols = signatures.all_column_symbols[
                    line_index:, column_index
                ]
                column_cases = signatures.all_column_cases[line_index:, column_index]
                column_lengths = signatures.all_column_character_lengths[
                    line_index:, column_index
                ]
                column_tokens = signatures.all_column_tokens[line_index:, column_index]
                column_values = signatures.all_normalized_values[
                    line_index:, column_index
                ]
                column_is_numeric_train = signatures.all_column_is_numeric_train[
                    line_index:, column_index
                ]

            candidate_tokens = set(
                [t for t in column_tokens[0] if any(c.isalpha() for c in t)]
            )

            patterns.data_initialize(
                column_index,
                candidate_value,
                candidate_tokens,
                column_values,
                column_tokens,
                column_train_sigs,
                column_bw_train_sigs,
                column_symbols,
                column_cases,
                column_lengths,
                column_is_numeric_train,
                args.max_summary_strength,
            )

            # patterns of a window INCLUDING the cell we are on
            data_patterns = patterns.data[column_index]
            value_pattern_summary, value_chain_consistent = data_patterns["train"]
            value_pattern_BW_summary, _ = data_patterns["bw_train"]
            value_symbol_summary = data_patterns["symbolset"]
            case_summary = data_patterns["case"]
            length_summary = data_patterns["character_length"]
            summary_strength = data_patterns["summary_strength"]
            candidate_count_for_value = data_patterns["candidate_count"][
                candidate_value
            ]
            partof_multiword_value_repeats = data_patterns[
                "partof_multiword_value_repeats"
            ]
            consistent_symbol_sets, _ = data_patterns["consistent_symbol_sets"]
            train_sigs_all_numeric, _ = data_patterns["column_is_numeric"]
            data_rules_fired[line_index][column_index][
                "summary_strength"
            ] = summary_strength

            if (
                null_equivalent_fired == True
                or len(value_pattern_summary) > 0
                or len(value_pattern_BW_summary) > 0
                or len(value_symbol_summary) > 0
                or len(case_summary) > 0
            ):
                all_summaries_empty = False

            for rule in fuzzy_rules["cell"]["data"].keys():
                rule_fired = False
                # Don't bother looking for agreements if there are no patterns or if the value on this line gives an empty pattern
                non_empty_patterns = 0
                # CHECK RULE
                if (
                    rule not in ignore_rules["cell"]["data"]
                    and len(column_train_sigs) > 0
                    and value_lower not in pat_util.null_equivalent_values
                ):
                    for pattern in column_train_sigs:
                        if pattern != []:
                            non_empty_patterns += 1

                    # there is no point calculating agreement over one value, a single value always agrees with itself.

                    if (len(column_train_sigs) >= 2 and non_empty_patterns >= 2) or (
                        len(csv_file.index) > 0 and line_index == csv_file.index[-1]
                    ):  ### TEST CHANGE
                        assert len(column_values) > 0

                        rule_fired = eval_data_cell_rule(
                            rule,
                            column_values,
                            column_tokens,
                            value_pattern_summary,
                            value_chain_consistent,
                            value_pattern_BW_summary,
                            value_symbol_summary,
                            column_symbols,
                            column_train_sigs,
                            case_summary,
                            candidate_count_for_value,
                            partof_multiword_value_repeats,
                            candidate_tokens,
                            consistent_symbol_sets,
                            train_sigs_all_numeric,
                            candidate_tokens,
                        )
                if rule_fired == True:
                    data_rules_fired[line_index][column_index]["agreements"].append(
                        rule
                    )

        data_rules_fired[line_index]["all_summaries_empty"] = all_summaries_empty

    ##########################################################################################
    ##########################################################################################
    #################             EVALUATE NOT_DATA CELL RULES             ###################
    ##########################################################################################
    ##########################################################################################
    # input('\nEVALUATE NOT_DATA CELL RULES')

    row_counter = -1
    for row in csv_file.itertuples():
        line_index = row.Index
        not_data_rules_fired[line_index] = {}
        if len(row) > 1:
            row = row[1:]
        else:
            row = []
        row_values = [str(elem) if elem is not None else elem for elem in row]

        row_counter += 1

        for columnindex, column in enumerate(csv_file.columns):

            not_data_rules_fired[line_index][columnindex] = {}
            not_data_rules_fired[line_index][columnindex]["disagreements"] = []
            candidate_value = signatures.all_normalized_values[line_index, columnindex]

            value_lower = candidate_value.lower()
            value_tokens = value_lower.split()

            column_train_sigs = None
            column_symbols = None
            column_cases = None
            column_lengths = None

            if args.max_summary_strength != None:
                nonempty_patterns = 0
                nonempty_patterns_idx = 0
                for nonempty_patterns_idx in range(
                    0, min(n_lines - (line_index + 1), args.max_line_depth)
                ):
                    if (
                        len(
                            signatures.all_column_train[
                                line_index + 1 + nonempty_patterns_idx, columnindex
                            ]
                        )
                        > 0
                    ):
                        nonempty_patterns += 1
                        if nonempty_patterns == args.max_summary_strength:
                            column_train_sigs = signatures.train_normalized_numbers[
                                line_index
                                + 1 : line_index
                                + 1
                                + nonempty_patterns_idx
                                + 1,
                                columnindex,
                            ].tolist()
                            column_bw_train_sigs = (
                                signatures.bw_train_normalized_numbers[
                                    line_index
                                    + 1 : line_index
                                    + nonempty_patterns_idx
                                    + 1,
                                    columnindex,
                                ].tolist()
                            )
                            column_symbols = signatures.symbolset_normalized_numbers[
                                line_index
                                + 1 : line_index
                                + 1
                                + nonempty_patterns_idx
                                + 1,
                                columnindex,
                            ]
                            column_cases = signatures.all_column_cases[
                                line_index
                                + 1 : line_index
                                + 1
                                + nonempty_patterns_idx
                                + 1,
                                columnindex,
                            ]
                            column_lengths = signatures.all_column_character_lengths[
                                line_index
                                + 1 : line_index
                                + 1
                                + nonempty_patterns_idx
                                + 1,
                                columnindex,
                            ]
                            break

            if column_train_sigs == None:
                column_train_sigs = signatures.train_normalized_numbers[
                    line_index + 1 :, columnindex
                ].tolist()
                column_bw_train_sigs = signatures.bw_train_normalized_numbers[
                    line_index + 1 :, columnindex
                ].tolist()
                column_symbols = signatures.symbolset_normalized_numbers[
                    line_index + 1 :, columnindex
                ]
                column_cases = signatures.all_column_cases[
                    line_index + 1 :, columnindex
                ]
                column_lengths = signatures.all_column_character_lengths[
                    line_index + 1 :, columnindex
                ]

            disagreement_summary_strength = sum(
                1 for x in column_train_sigs if len(x) > 0
            )
            not_data_rules_fired[line_index][column][
                "disagreement_summary_strength"
            ] = disagreement_summary_strength

            cand_pattern = signatures.train_normalized_numbers[line_index, columnindex]
            cand_symbols = signatures.symbolset_normalized_numbers[
                line_index, columnindex
            ]
            cand_case = signatures.all_column_cases[line_index, columnindex]
            cand_num_chars = signatures.all_column_character_lengths[
                line_index, columnindex
            ]

            (
                value_pattern_summary,
                value_chain_consistent,
            ) = pat_util.generate_pattern_summary(column_train_sigs)
            value_pattern_BW_summary, _ = pat_util.generate_pattern_summary(
                column_bw_train_sigs
            )
            value_symbol_summary = pat_util.generate_symbol_summary(column_symbols)
            case_summary = pat_util.generate_case_summary(column_cases)
            length_summary = pat_util.generate_length_summary(column_lengths)

            if len(cand_pattern) > 0:
                columnvalues = signatures.all_normalized_values[
                    line_index:, columnindex
                ]
                repetitions_of_candidate = (columnvalues[1:] == candidate_value).sum()
                neighbor = ""
                try:
                    neighbor = columnvalues[1]
                    repetitions_of_neighbor = (columnvalues[2:] == neighbor).sum()
                except:
                    repetitions_of_neighbor = 0

            for rule in fuzzy_rules["cell"]["not_data"].keys():
                rule_fired = False
                if (
                    rule not in ignore_rules["cell"]["not_data"]
                    and len(cand_pattern) > 0
                ):
                    if disagreement_summary_strength > 0 and (
                        np.all(signatures.all_column_isnumber[line_index:, columnindex])
                        == False
                    ):
                        rule_fired = eval_not_data_cell_rule(
                            rule,
                            repetitions_of_candidate,
                            repetitions_of_neighbor,
                            neighbor,
                            value_pattern_summary,
                            value_pattern_BW_summary,
                            value_chain_consistent,
                            value_symbol_summary,
                            case_summary,
                            length_summary,
                            cand_pattern,
                            cand_symbols,
                            cand_case,
                            cand_num_chars,
                            disagreement_summary_strength,
                            data_rules_fired,
                            columnindex,
                            line_index,
                        )
                        if rule_fired == True:
                            not_data_rules_fired[line_index][columnindex][
                                "disagreements"
                            ].append(rule)
        # end processing column
        ########################################################################
        #### COLLECT LINE RULES ####

        # 1. Collect data line rules fired
        line_is_data_events = assess_data_line(row_values)
        data_rules_fired[line_index]["line"] = []
        not_data_rules_fired[line_index]["line"] = []

        for rule in fuzzy_rules["line"]["data"].keys():
            rule_fired = False
            if rule not in ignore_rules["line"]["data"] and rule in line_is_data_events:
                rule_fired = True
                data_rules_fired[line_index]["line"].append(rule)

        # 2.  Collect not_data line rules fired

        # non_nulls,non_null_percentage = non_nulls_in_line(row_values)
        all_summaries_empty = data_rules_fired[line_index]["all_summaries_empty"]
        header_events_fired = collect_events_on_row(row_values)
        arithmetic_events_fired = collect_arithmetic_events_on_row(row_values)

        arithmetic_sequence_fired = False
        if len(arithmetic_events_fired) > 0:
            arithmetic_sequence_fired = True
        header_row_with_aggregation_tokens_fired = header_row_with_aggregation_tokens(
            row_values, arithmetic_sequence_fired
        )

        before_data = True
        not_data_line_rules_fired = []
        if csv_file.shape[1] > 1:
            not_data_line_rules_fired = assess_non_data_line(
                row_values, before_data, all_summaries_empty, line_index, csv_file
            )

        for rule in fuzzy_rules["line"]["not_data"].keys():
            rule_fired = False
            if rule not in ignore_rules["line"]["not_data"] and rule in (
                not_data_line_rules_fired
                + header_events_fired
                + arithmetic_events_fired
                + header_row_with_aggregation_tokens_fired
            ):
                rule_fired = True
                not_data_rules_fired[line_index]["line"].append(rule)

    return data_rules_fired, not_data_rules_fired


def combined_table_confidence(top, bottom):
    return min(top, bottom)


def convert(o):
    if isinstance(o, np.int64):
        return int(o)
    raise TypeError


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["infer", "train"])
    parser.add_argument(
        "-w", "--weights", default=None
    )  # , description="Filepath to pre-trained rule weights")
    parser.add_argument(
        "-f", "--filepath", default=None
    )  # , description="Filepath to CSV file over which to infer annotations")
    parser.add_argument("-o", "--output_file", default=None)
    parser.add_argument(
        "-c", "--csv_files", default=None
    )  # , description="Filepath to folder with CSV training files")
    parser.add_argument(
        "-a", "--annotations", default=None
    )  # , description="Filepath to folder with JSON annotations over CSV training files")

    args = parser.parse_args(sys.argv[1:])
    command = args.command
    weights = args.weights
    filepath = args.filepath
    csv_files = args.csv_files
    annotations = args.annotations
    output_file = args.output_file

    if command == "infer":
        if weights is None or filepath is None:
            sys.exit()
        else:
            Pytheas = API()
            Pytheas.load_weights(weights)
            infered_annotations = Pytheas.infer_annotations(filepath)
            pp.pprint(infered_annotations)
            if output_file is not None:
                with open(output_file, "w") as outfile:
                    json.dump(infered_annotations, outfile, default=convert)
    elif command == "train":
        if csv_files is None or annotations is None or output_file is None:
            sys.exit()
        else:
            Pytheas = API()
            Pytheas.learn_and_save_weights(csv_files, annotations, output_file)
