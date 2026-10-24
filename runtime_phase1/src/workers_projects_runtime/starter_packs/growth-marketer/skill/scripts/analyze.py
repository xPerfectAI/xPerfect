"""Original, local-only calculation aid for a reviewable expert work product.

Inputs remain unchanged. No external service, account, install or publication is used.
"""
import argparse,csv,hashlib,json,math,statistics
from pathlib import Path


def rows(path,required):
    with Path(path).open(newline='',encoding='utf-8-sig') as handle:
        reader=csv.DictReader(handle)
        if not set(required).issubset(reader.fieldnames or []):
            raise ValueError('Input columns do not match the documented contract: '+', '.join(required))
        result=list(reader)
    if not result:
        raise ValueError('The input has no data rows')
    return result


def number(value,label):
    result=float(value)
    if not math.isfinite(result):
        raise ValueError(label+' must be finite')
    return result


def unique(values,label):
    if any(not str(v).strip() for v in values) or len(values)!=len(set(values)):
        raise ValueError(label+' must be present and unique; resolve the source before analysis')


def save_csv(path,records):
    if not records:return
    with path.open('w',newline='',encoding='utf-8') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(records[0]));writer.writeheader()
        for record in records:
            safe={k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in record.items()}
            writer.writerow(safe)


def analyze(args,plan,out):
    data=rows(args.data,['period','channel','currency','spend','visits','leads','qualified_leads','customers','revenue'])
    unique([(r['period'],r['channel']) for r in data],'Period/channel pairs')
    if plan.get('attribution')!='mutually_exclusive':raise ValueError('Resolve attribution overlap before aggregating channels')
    results=[]
    for row in data:
        if row['currency']!=plan['currency'] or row['period']!=plan['period']:raise ValueError('Currency and reporting period must match the measurement plan')
        values={k:number(row[k],k) for k in ['spend','visits','leads','qualified_leads','customers','revenue']}
        if min(values.values())<0:raise ValueError('Marketing measures must be nonnegative')
        if not values['visits']>=values['leads']>=values['qualified_leads']>=values['customers']:raise ValueError('Funnel stages do not reconcile')
        if any(not values[k].is_integer() for k in ['visits','leads','qualified_leads','customers']):raise ValueError('Funnel counts must be whole numbers')
        ratio=lambda a,b:a/b if b else None
        results.append({'channel':row['channel'],**values,'cost_per_qualified_lead':ratio(values['spend'],values['qualified_leads']),'cost_per_customer':ratio(values['spend'],values['customers']),'lead_to_customer_rate':ratio(values['customers'],values['leads']),'revenue_to_spend':ratio(values['revenue'],values['spend'])})
    save_csv(out/'channel-performance.csv',results)
    return {'status':'review_required','period':plan['period'],'currency':plan['currency'],'channels':results,'scenario_budget':plan['scenario_budget'],'limitations':['Historical attribution is the supplied assumption, not independently established causality.','A proposed campaign/budget is a scenario; no publishing, contact or spending was performed.'],'files':['channel-performance.csv']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True)
    parser.add_argument('--plan',required=True)
    parser.add_argument('--output-dir',required=True)
    args=parser.parse_args();out=Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        print(json.dumps({'status':'needs_input','reason':'Choose an empty output folder; existing work was preserved','analysis_complete':False}));return 2
    out.mkdir(parents=True,exist_ok=True)
    try:
        plan=json.loads(Path(args.plan).read_text(encoding='utf-8'))
        result=analyze(args,plan,out)
        result['input_sha256']={Path(p).name:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in [args.data,args.plan]}
        (out/'analysis.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        print(json.dumps({'status':result['status'],'output':'analysis.json'},allow_nan=False))
        return 0
    except (ValueError,KeyError,TypeError,OSError,csv.Error) as error:
        result={'status':'needs_input','reason':str(error),'analysis_complete':False}
        (out/'input-issue.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result));return 2


if __name__=='__main__':
    raise SystemExit(main())
