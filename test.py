from pytheas.pytheas import API
import pprint as pp
filepath = "../greenfield-csv/data/examples/cbp19us.txt"
weights = "pytheas/trained_rules.json"
Pytheas = API(weights=weights)
infered_annotations = Pytheas.infer_annotations(filepath, max_lines=100)
pp.pprint(infered_annotations)



""" pip 
2 "DATES: Oct 17-20, 2019",,
3 METHOD: T/I,,
4 "SAMPLE SIZE: 1,994",,
5 PARTY, LEAD_NAME, PROJ_SUPPORT
LIB*, Justin Trudeau, 34
CON, Andrew Scheer, 30
NDP, Jagmeet Singh, 18
RNK, Elizabeth May, 8
BQ, Yves-François Blanchet, 5
NOT PREDICTED TO WIN RIDINGS,,
OTH, nd, 1
(MOE):+/-2.2%,,
* Currently in government.,,

{'blanklines': [],
 'columns_in_file': 3,
 'columns_in_file_considered': 3,
 'lines_processed': 14,
 'tables': [{'bottom_boundary': 13,
             'columns': {0: {'column_header': [{'column': 0,
                                                'index': 0,
                                                'row': 4,
                                                'value': 'PARTY'}],
                             'table_column': 0},
                         1: {'column_header': [{'column': 1,
                                                'index': 0,
                                                'row': 4,
                                                'value': 'LEAD_NAME'}],
                             'table_column': 1},
                         2: {'column_header': [{'column': 2,
                                                'index': 0,
                                                'row': 4,
                                                'value': 'PROJ_SUPPORT'}],
                             'table_column': 2}},
             'confidence': {'body': 0.5224123611765388,
                            'body_end': 0.5224123611765388,
                            'body_start': 0.9786323074912059},
             'data_end': 11,
             'data_start': 5,
             'footnotes': [12, 13],
             'header': [4],
             'subheaders': [],
             'table_counter': 1,
"""