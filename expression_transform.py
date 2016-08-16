#!/usr/bin/python

import argparse
import pandas as pd
import json
import sys
import numpy as np
import requests
import os
import uuid
import csv
from scipy import stats
from itertools import islice

#requires 2.7.9 or greater to deal with https comodo intermediate certs
if sys.version_info < (2, 7):
        raise "must use python 2.7 or greater"

#stamp out annoying warnings that are beyond control
import warnings
warnings.simplefilter(action = "ignore", category = FutureWarning)
pd.options.mode.chained_assignment = None

#Input
#1. metadata in json with the following:

"""
{xformat:"csv || tsv || xls ||  xlsx",
xsetup:"gene_matrix || gene_list",
source_id_type:"refseq_locus_tag || alt_locus_tag || feature_id || gi || gene_id || protein_id || seed_id", 
data_type: "Transcriptomics || Proteomics || Phenomics", 
title: "User input", 
desc: "User input",
organism: "user input", 
pmid: "user_input",
output_path: "path",
"metadata_format":"csv || tsv || xls ||  xlsx"}
"""
#2. server info for the data api
"""
{"data_api":"url"}
"""

#Sample Output
#experiment.json
#{"origFileName":"filename","geneMapped":4886,"samples":8,"geneTotal":4985,"cdate":"2013-01-28 13:40:47","desc":"user input","organism":"some org","owner":"user name","title":"user input","pmid":"user input","expid":"whatever","collectionType":"ExpressionExperiment","genesMissed":99,"mdate":"2013-01-28 13:40:47"}

#expression.json
#{"expression":[{"log_ratio":"0.912","na_feature_id":"36731006","exp_locus_tag":"VBISalEnt101322_0001","pid":"8f2e7338-9f04-4ba5-9fe2-5365c857d57fS0","z_score":"-0.23331085637221843"}]

#mapping.json
#{"mapping":{"unmapped_list":[{"exp_locus_tag":"VBISalEnt101322_pg001"}],"unmapped_ids":99,"mapped_list":[{"na_feature_id":"36731006","exp_locus_tag":"VBISalEnt101322_0001"}],"mapped_ids":4886}}

#sample.json
#{"sample":[{"sig_log_ratio":2675,"expmean":"1.258","sampleUserGivenId":"LB_stat_AerobicM9_stat_aerobic","expname":"LB_stat_AerobicM9_stat_aerobic","pid":"8f2e7338-9f04-4ba5-9fe2-5365c857d57fS0","genes":4429,"sig_z_score":139,"expstddev":"1.483"}]}


def pretty_print_POST(req):
    """
    printed and may differ from the actual request.
    """
    print('{}\n{}\n{}\n\n{}'.format(
        '-----------START-----------',
        req.method + ' ' + req.url,
        '\n'.join('{}: {}'.format(k, v) for k, v in req.headers.items()),
        req.body,
    ))

#convert gene list format to gene matrix
#there is definitely a more efficient conversion than this...
def gene_list_to_matrix(cur_table):
    comparisons=set(cur_table['sampleUserGivenId'])
    genes=set(cur_table['exp_locus_tag'])
    result=pd.DataFrame(index=sorted(list(genes)), columns=sorted(list(comparisons)))
    result['exp_locus_tag']=result.index
    gene_pos=cur_table.columns.get_loc('exp_locus_tag')
    comparison_pos=cur_table.columns.get_loc('sampleUserGivenId')
    ratio_pos=cur_table.columns.get_loc('log_ratio')
    for row in cur_table.iterrows():
        gene_id=row[-1][gene_pos]
        comp=row[-1][comparison_pos]
        ratio=row[-1][ratio_pos]
        result[comp][gene_id]=ratio
    return result

#convert gene matrix format to gene list
#there is definitely a more efficient conversion than this...
def gene_matrix_to_list(cur_table):
    result=pd.melt(cur_table, id_vars=['exp_locus_tag'], var_name='sampleUserGivenId', value_name='log_ratio')
    return result

def list_to_mapping_table(cur_table):
    genes=set(cur_table['exp_locus_tag'])
    result=pd.DataFrame(index=sorted(list(genes)))
    result['exp_locus_tag']=result.index
    return result

#deal with weird naming of columns.
def fix_headers(cur_table, parameter_type, die):
    def fix_name(x, all_columns): 
        fixed_name=' '.join(x.split()).strip().lower().replace(" ","_")
        #patrics downloadable template is not consistent with its help info
        if fixed_name.endswith('s') and fixed_name[:-1] in set(all_columns):
            fixed_name=fixed_name[:-1]
        return fixed_name

    matrix_columns=['gene_id']
    list_columns=['gene_id', 'comparison_id', 'log_ratio']
    template_columns=["comparison_id","title","pubmed","accession","organism","strain","gene_modification","experiment_condition","time_point"]
    all_columns=list_columns+template_columns
    check_columns=None
    target_setup=None
    if parameter_type=="xfile":
        target_setup= "gene_list" if all([(fix_name(x,all_columns) in list_columns) for x in cur_table.columns]) else "gene_matrix"
    else:
	target_setup="template"
    limit_columns=True
    if target_setup == 'gene_matrix':
        check_columns=matrix_columns
        limit_columns=False
        rename={'gene_id': 'exp_locus_tag'}
    elif target_setup == 'gene_list':
        check_columns=list_columns
        rename={'comparison_id':'sampleUserGivenId','gene_id': 'exp_locus_tag'}
    elif target_setup == 'template':
        check_columns=template_columns
        rename={'comparison_id':'sampleUserGivenId', 'title':'expname', 'gene_modification':'mutant', 'experiment_condition':'condition', 'time_point':'timepoint'}
    else:
        sys.stderr.write("unrecognized setup "+target_setup+"\n")
        if die: assert False
    cur_table.columns=[fix_name(x,all_columns) if fix_name(x,all_columns) in check_columns else x for x in cur_table.columns]
    columns_ok = True
    for i in check_columns:
        columns_ok=columns_ok and i in cur_table.columns
    if not columns_ok:
            sys.stderr.write("Missing appropriate column names in "+target_setup+"\n")
            if die: assert False
    if limit_columns:
        cur_table=cur_table[check_columns]
    if rename:
       cur_table=cur_table.rename(columns=rename)
    return (target_setup, cur_table)

#read in the comparisons data and metadata
def process_table(target_file, param_type, die, target_format="start", tries=0):
    tries+=1
    starting=False
    target_setup=None
    if not os.path.exists(target_file):
        sys.stderr.write("can't find target file "+target_file+"\n")
        if die: sys.exit(2)
    if target_format=="start":
        starting=True
        fileName, fileExtension = os.path.splitext(target_file)
        target_format=fileExtension.replace('.','').lower()
    if starting and not target_format in set(["csv","tsv","xls","xlsx"]):
	temp_handle=open(target_file, 'rb')
	target_sep=csv.Sniffer().sniff("\n".join(list(islice(temp_handle,10))))
        temp_handle.close()
	if target_sep.delimiter=="\t":
		target_format="tsv"
                sys.stdout.write("guessing "+target_format+" format\n")
	elif target_sep.delimiter==",":
		target_format="csv"
                sys.stdout.write("guessing "+target_format+" format\n")
		
    cur_table=None
    next_up="tsv"
    try:
        if target_format == 'tsv':
            next_up="csv"
            cur_table=pd.read_table(target_file, header=0)
        elif target_format == 'csv':
            next_up="xls"
            cur_table=pd.read_csv(target_file, header=0)
        elif target_format == 'xls' or target_format == 'xlsx':
            cur_table=pd.io.excel.read_excel(target_file, 0, index_col=None)
        else:
            sys.stderr.write("unrecognized format "+target_format+" for "+target_setup+"\n")
            if die: sys.exit(2)
    	target_setup, cur_table=fix_headers(cur_table, param_type, die)
    except: 
        sys.stdout.write("failed at reading "+target_format+" format\n")
        if tries > 5:
            raise
	else:
            sys.stdout.write("guessing "+next_up+" format\n")
            return process_table(target_file, param_type, die, next_up, tries)
    return (target_setup, cur_table)

#{source_id_type:"refseq_locus_tag || alt_locus_tag || feature_id", 
#data_type: "Transcriptomics || Proteomics || Phenomics", 
#experiment_title: "User input", experiment_description: "User input",
#organism name: "user input", pubmed_id: "user_input"}

#Sample Output
#experiment.json
#{"origFileName":"filename","geneMapped":4886,"samples":8,"geneTotal":4985,"cdate":"2013-01-28 13:40:47","desc":"user input","organism":"some org","owner":"user name","title":"user input","pmid":"user input","expid":"whatever","collectionType":"ExpressionExperiment","genesMissed":99,"mdate":"2013-01-28 13:40:47"}
def create_experiment_file(output_path, mapping_dict, sample_dict, expression_dict, form_data, experiment_id):
    experiment_dict={"geneMapped":mapping_dict["mapping"]["mapped_ids"],"samples":len(sample_dict['sample']),"geneTotal":mapping_dict["mapping"]["mapped_ids"]+mapping_dict["mapping"]["unmapped_ids"],"desc":(form_data.get('desc','')),"organism":form_data.get('organism',''),"title":form_data.get("title",""),"pmid":form_data.get("pmid",""),"expid":experiment_id,"collectionType":"ExpressionExperiment","genesMissed":mapping_dict["mapping"]["unmapped_ids"]}
    output_file=os.path.join(output_path, 'experiment.json')
    out_handle=open(output_file, 'w')
    json.dump(experiment_dict, out_handle)
    out_handle.close()
    return experiment_dict
    

#expression.json
#{"expression":[{"log_ratio":"0.912","na_feature_id":"36731006","exp_locus_tag":"VBISalEnt101322_0001","pid":"8f2e7338-9f04-4ba5-9fe2-5365c857d57fS0","z_score":"-0.23331085637221843"}]
 
#sample.json
#{"sample":[{"sig_log_ratio":2675,"expmean":"1.258","sampleUserGivenId":"LB_stat_AerobicM9_stat_aerobic","expname":"LB_stat_AerobicM9_stat_aerobic","pid":"8f2e7338-9f04-4ba5-9fe2-5365c857d57fS0","genes":4429,"sig_z_score":139,"expstddev":"1.483"}]}

def create_comparison_files(output_path, comparisons_table, mfile, form_data, experiment_id, sig_z, sig_log):
    #create dicts for json
    sample_dict={'sample':[]}
    expression_dict={'expression':[]}
    #create stats table for sample.json
    grouped=comparisons_table.groupby(["sampleUserGivenId"])
    sample_stats=grouped.agg([np.mean, np.std])['log_ratio']
    sample_stats=sample_stats.rename(columns={'mean':'expmean','std':'expstddev'})
    sample_stats["genes"]=grouped.count()["exp_locus_tag"]
    sample_stats["pid"]=[str(experiment_id)+"S"+str(i) for i in range(0,len(sample_stats))]
    sample_stats["sampleUserGivenId"]=sample_stats.index
    sample_stats["expname"]=sample_stats.index
    #get zscore and significance columns
    comparisons_table["z_score"]=grouped.transform(stats.zscore)
    comparisons_table["sig_z"]=comparisons_table["z_score"].abs() >= sig_z
    comparisons_table["sig_log"]=comparisons_table["log_ratio"].abs() >= sig_log
    #store counts in stats
    z_score_breakdown=comparisons_table.groupby(["sampleUserGivenId","sig_z"]).count()['z_score'].unstack()
    if True in z_score_breakdown:
        sample_stats["sig_z_score"]=z_score_breakdown[True]
    else:
        z_score_breakdown.columns=[True]
        z_score_breakdown[True]=z_score_breakdown[True].apply(lambda x: 0)
        sample_stats["sig_z_score"]=z_score_breakdown[True]

    log_breakdown=comparisons_table.groupby(["sampleUserGivenId","sig_log"]).count()['log_ratio'].unstack()
    if True in log_breakdown:
        sample_stats["sig_log_ratio"]=log_breakdown[True]
    else:
        log_breakdown.columns=[True]
        log_breakdown[True]=log_breakdown[True].apply(lambda x: 0)
        sample_stats["sig_log_ratio"]=log_breakdown[True]
    sample_stats["sig_log_ratio"]=sample_stats["sig_log_ratio"].fillna(0).astype('int64')
    sample_stats["sig_z_score"]=sample_stats["sig_z_score"].fillna(0).astype('int64')
    #set pid's for expression.json
    comparisons_table=comparisons_table.merge(sample_stats[["pid","sampleUserGivenId"]], how="left", on="sampleUserGivenId")
    #pull in metadata spreadsheet if provided
    if mfile and mfile.strip():
        sys.stdout.write("reading metadata template\n")
        target_setup, meta_table=process_table(mfile, "mfile", die=True)
        try:
            meta_key="sampleUserGivenId"
            to_add=meta_table.columns-sample_stats.columns
            meta_table=meta_table.set_index('sampleUserGivenId')
            sample_stats.update(meta_table)
            sample_stats=sample_stats.merge(meta_table[to_add], left_index=True, right_index=True, how='left')
        except:
            sys.stderr.write("failed to parse user provide metadata template\n")
            sys.exit(2)
    #populate json dicts
    sample_stats=sample_stats.fillna("")
    sample_dict['sample']=json.loads(sample_stats.to_json(orient='records', date_format='iso'))
    #sample_dict['sample']=sample_stats.to_dict(outtype='records')
    cols = [col for col in comparisons_table.columns if col not in ['sig_z', 'sig_log']]
    expression_dict['expression']=json.loads(comparisons_table[cols].to_json(orient='records'))
    output_file=os.path.join(output_path, 'sample.json')
    out_handle=open(output_file, 'w')
    json.dump(sample_dict, out_handle)
    out_handle.close()
    output_file=os.path.join(output_path, 'expression.json')
    out_handle=open(output_file, 'w')
    json.dump(expression_dict, out_handle)
    out_handle.close()
    return (sample_dict, expression_dict)
    
    

#mapping.json
#{"mapping":{"unmapped_list":[{"exp_locus_tag":"VBISalEnt101322_pg001"}],"unmapped_ids":99,"mapped_list":[{"na_feature_id":"36731006","exp_locus_tag":"VBISalEnt101322_0001"}],"mapped_ids":4886}}

#creates mapping.json for results
def create_mapping_file(output_path, mapping_table, form_data):
    mapping_dict={"mapping":{"unmapped_list":[],"unmapped_ids":0,"mapped_list":[],"mapped_ids":0}}
    mapping_dict['mapping']['unmapped_list']=mapping_table[mapping_table.isnull().any(axis=1)][['exp_locus_tag']].to_dict(outtype='records')
    mapping_dict['mapping']['mapped_list']=mapping_table[mapping_table.notnull().all(axis=1)].to_dict(outtype='records')
    mapping_dict['mapping']['unmapped_ids']=len(mapping_dict['mapping']['unmapped_list'])
    mapping_dict['mapping']['mapped_ids']=len(mapping_dict['mapping']['mapped_list'])
    output_file=os.path.join(output_path, 'mapping.json')
    out_handle=open(output_file, 'w')
    json.dump(mapping_dict, out_handle)
    out_handle.close()
    return mapping_dict

    #mapped_list=[{form_data["source_id_type"]: i["Map ID"], "exp_locus_tag":i['Gene ID']} for i in mapping_table[mapping_table.notnull().any(axis=1)]]
    #mapped_list=[{form_data["source_id_type"]: i["Map ID"], "exp_locus_tag":i["Gene ID"]} for i in mapping_table.query('Gene ID != @np.nan')]


def place_ids(query_results,cur_table,form_data):
    source_types=form_data["source_types"]+form_data["int_types"]
    count=0
    try:
        for d in query_results.json()['response']['docs']:
            source_ids=[]
            target_id=None
            for id_type in source_types:
                if id_type in d:
                    source_ids.append(d[id_type])
            
            if 'feature_id' in d:
                target_id=d['feature_id']
            if target_id:
                #because which of the source id's are in the input data check them locally against the exp_locus_tag
                for source_id in source_ids:
                    if source_id in cur_table["feature_id"]:
                        count+=1
                        cur_table["feature_id"][source_id]=target_id
                        break
    except ValueError:
        sys.stderr.write("mapping failed. either PATRICs API is down or the Gene IDs are unknown\n")
        raise
    if count==0:
        sys.stderr.write("mapping failed. either PATRICs API is down or the Gene IDs are unknown\n")
        sys.exit(2)

def make_map_query(id_list, form_data, server_setup, chunk_size):
    source_types=form_data["source_types"]
    int_types=form_data["int_types"]
    current_query={'q':""}
    map_queries=[]
    int_ids=[]
    for id in id_list:
        if id.isdigit():
            int_ids.append(id)
    if len(int_ids):
        for s_type in int_types:
            map_queries.append("("+s_type+":("+" OR ".join(int_ids)+"))")
    for s_type in source_types:
        map_queries.append("("+s_type+":("+" OR ".join(id_list)+"))")
    current_query["q"]+="("+" OR ".join(map_queries)+") AND annotation:PATRIC"
    if "genome_id" in form_data and form_data["genome_id"]:
        current_query["q"]+=" AND genome_id:"+form_data["genome_id"]
    current_query["fl"]="feature_id,"+",".join(source_types)
    current_query["rows"]="20000"
    current_query["wt"]="json"
    headers = {"Content-Type": "application/solrquery+x-www-form-urlencoded", "accept":"application/solr+json"}
    #print "switch THE HEADER BACK!"
    #headers = {'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'}
    req = requests.Request('POST', server_setup["data_api"], headers=headers, data=current_query)
    prepared = req.prepare()
    #pretty_print_POST(prepared)
    s = requests.Session()
    response=s.send(prepared)
    if not response.ok:
        sys.stderr.write("Mapping API not responding. Please try again later.\n")
        sys.exit(2)
    return response


def chunker(seq, size):
    return (seq[pos:pos + size] for pos in xrange(0, len(seq), size))

def map_gene_ids(cur_table, form_data, server_setup):
    cur_table["feature_id"]=np.nan
    chunk_size=1000
    for i in chunker(cur_table['exp_locus_tag'], chunk_size):
        mapping_results=make_map_query(i, form_data, server_setup, chunk_size)
        place_ids(mapping_results, cur_table, form_data)


def main():
    sig_z=2
    sig_log=1
    valid_formats=set(['csv', 'tsv', 'xls', 'xlsx'])
    valid_setups=set(['gene_matrix','gene_list'])
    
    #req_info=['xformat','xsetup','source_id_type','data_type','experiment_title','experiment_description','organism']
    req_info=['data_type','experiment_title','experiment_description','organism']
    

    parser = argparse.ArgumentParser()
    parser.add_argument('--xfile', help='the source Expression comparisons file', required=True)
    parser.add_argument('--mfile', help='the metadata template if it exists', required=False)
    parser.add_argument('--output_path', help='location for output', required=True)
    userinfo = parser.add_mutually_exclusive_group(required=True)
    userinfo.add_argument('--ufile', help='json file from user input')
    userinfo.add_argument('--ustring', help='json string from user input')
    serverinfo = parser.add_mutually_exclusive_group(required=True)
    serverinfo.add_argument('--sfile', help='server setup JSON file')
    serverinfo.add_argument('--sstring', help='server setup JSON string')
    args = parser.parse_args()
    if len(sys.argv) ==1:
        parser.print_help()
        sys.exit(2)

    #get comparison and metadata files
    xfile=args.xfile
    mfile=args.mfile if 'mfile' in args else None

    #parse user form data
    form_data=None
    user_parse=None
    server_parse=None
    parse_server = json.loads if 'sstring' in args else json.load
        
    try:
        form_data = json.loads(args.ustring) if args.ustring else json.load(open(args.ufile,'r'))
    except:
        sys.stderr.write("Failed to parse user provided form data \n")
        raise
    #parse setup data
    try:
        server_setup= json.loads(args.sstring) if args.sstring else json.load(open(args.sfile,'r'))
    except:
        sys.stderr.write("Failed to parse server data\n")
        raise

    #part of auto-detection of id type add source id types to map from
    form_data["source_types"]=["refseq_locus_tag","alt_locus_tag","feature_id","protein_id","patric_id"]#,"gi"]
    form_data["int_types"]=["gi"]

    #make sure all required info present
    missing=[x not in form_data for x in req_info]
    if (any(missing)):
        sys.stderr.write("Missing required user input data: "+" ".join([req_info[i] for i in range(len(missing)) if missing[i]])+"\n")
        sys.exit(2)
    #if (mfile or 'metadata_format' in form_data) and ('metadata_format' not in form_data or not mfile):
    #    sys.stderr.write("Expression transformation: (file,format) pair must be given for metadata template\n")
        #sys.exit(2)


    #read comparisons file
    sys.stdout.write("reading comparisons file\n")
    target_setup, comparisons_table=process_table(xfile, "xfile", die=True)

    output_path=args.output_path

    #convert gene matrix to list
    if target_setup == 'gene_matrix':
        comparisons_table=gene_matrix_to_list(comparisons_table)

    #limit log ratios
    comparisons_table.ix[comparisons_table["log_ratio"] > 1000000, 'log_ratio']=1000000
    comparisons_table.ix[comparisons_table["log_ratio"] < -1000000, 'log_ratio']=-1000000
    comparisons_table=comparisons_table.dropna()

    #map gene ids
    mapping_table=list_to_mapping_table(comparisons_table)
    map_gene_ids(mapping_table, form_data, server_setup)
    comparisons_table=comparisons_table.merge(mapping_table, how='left', on='exp_locus_tag')

    #create json files to represent experiment
    experiment_id=str(uuid.uuid1())
    mapping_dict=create_mapping_file(output_path, mapping_table, form_data)
    (sample_dict, expression_dict) = create_comparison_files(output_path, comparisons_table, mfile, form_data, experiment_id, sig_z, sig_log)
    experiment_dict=create_experiment_file(output_path, mapping_dict, sample_dict, expression_dict, form_data, experiment_id)
    sys.stdout.write(json.dumps(experiment_dict)+"\n")
    
     
if __name__ == "__main__":
    main()
