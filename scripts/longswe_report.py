"""Aggregate every preselected paired run; never drop failed model runs."""
import argparse
import json
from pathlib import Path


def report(root,run_name):
    selection=json.loads((root/'selection.json').read_text())
    rows=[]
    for task in selection:
        paired={variant:json.loads((root/task['instance_id']/run_name/variant/'result.json').read_text())
                for variant in ('raw','managed')}
        raw,managed=paired['raw'],paired['managed']
        rows.append({'instance_id':task['instance_id'],'dataset_tokens':task['num_tokens'],
            'raw':raw,'managed':managed,
            'mean_input_reduction':1-managed['mean_main_input']/raw['mean_main_input']})
    totals={}
    for variant in ('raw','managed'):
        inputs=[value for row in rows for value in row[variant]['main_input_tokens']]
        complete=all(row[variant]['usage_complete'] for row in rows) and all(value is not None for value in inputs)
        totals[variant]={'main_requests':len(inputs),
                         'main_input_total':sum(value for value in inputs if value is not None),
                         'mean_main_input':sum(value for value in inputs if value is not None)/len(inputs),
                         'all_input_total':sum(row[variant]['all_usage']['input_tokens'] for row in rows),
                         'all_output_total':sum(row[variant]['all_usage']['output_tokens'] for row in rows),
                         'tools':sum(row[variant]['tools'] for row in rows),
                         'passed_tasks':sum(row[variant]['passed'] for row in rows),
                         'accepted_final_patches':sum(row[variant]['acceptance_exit']==0 for row in rows),
                         'request_and_pairs_preserved':all(row[variant]['request_and_pairs_preserved'] for row in rows),
                         'main_response_usage_available':all(value is not None for value in inputs),
                         'usage_complete':complete}
    value={'run_name':run_name,'selection':selection,'rows':rows,'totals':totals,
           'pooled_mean_input_reduction':1-totals['managed']['mean_main_input']/totals['raw']['mean_main_input'],
           'mean_of_task_reductions':sum(row['mean_input_reduction'] for row in rows)/len(rows),
           'main_total_input_reduction':1-totals['managed']['main_input_total']/totals['raw']['main_input_total'],
           'including_summary_input_reduction':1-totals['managed']['all_input_total']/totals['raw']['all_input_total'],
           'method':'3 preselected official 128K tasks; one run per condition; both share tool preview limits; '
                    'only summary compaction differs. Local original test patches and selected original test ids; '
                    'not official Docker scoring. Primary metric pools returned backend input usage across main requests. '
                    'Reported totals exclude attempts whose usage was unavailable; inspect usage_complete before cost claims.'}
    (root/f'summary-{run_name}.json').write_text(json.dumps(value,ensure_ascii=False,indent=2))
    print(json.dumps({key:value[key] for key in value if key not in ('rows','selection')},ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--run-name',required=True)
    args=parser.parse_args()
    report(args.root.resolve(),args.run_name)
