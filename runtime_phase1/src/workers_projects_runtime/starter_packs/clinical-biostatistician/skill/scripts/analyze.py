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


def wilson(success,n):
    p=success/n;z=statistics.NormalDist().inv_cdf(.975)
    den=1+z*z/n;center=(p+z*z/(2*n))/den
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return center-half,center+half


def analyze(args,plan,out):
    data=rows(args.data,['participant_id','arm','response'])
    unique([r['participant_id'] for r in data],'Participant IDs')
    arms=plan['arms']
    if not isinstance(arms,list) or len(arms)!=2 or len(set(arms))!=2:
        raise ValueError('The agreed plan must name two distinct arms')
    if plan.get('endpoint_type')!='binary' or plan.get('missing_policy')!='exclude_with_warning':
        raise ValueError('This aid supports only the explicitly agreed binary, observed-case analysis; another plan needs a different method')
    groups=[]
    for arm in arms:
        selected=[r for r in data if r['arm']==arm];observed=[]
        for r in selected:
            if r['response'].strip()=='':continue
            v=number(r['response'],'Response')
            if v not in (0,1):raise ValueError('Binary responses must be 0 or 1')
            observed.append(v)
        if not observed:raise ValueError('Each planned arm needs observed responses')
        n=len(observed);events=int(sum(observed));lo,hi=wilson(events,n)
        groups.append({'arm':arm,'assigned':len(selected),'observed':n,'missing':len(selected)-n,'responses':events,'response_rate':events/n,'ci95_low':lo,'ci95_high':hi})
    if any(r['arm'] not in arms for r in data):raise ValueError('Data contains an arm not in the agreed plan')
    a,b=groups;p0,p1=a['response_rate'],b['response_rate'];difference=p1-p0
    low=difference-math.sqrt((p1-b['ci95_low'])**2+(a['ci95_high']-p0)**2)
    high=difference+math.sqrt((b['ci95_high']-p1)**2+(p0-a['ci95_low'])**2)
    save_csv(out/'group-results.csv',groups)
    return {'status':'review_required','question':plan['question'],'method':'Observed-case binary response; Wilson95 intervals and Newcombe difference interval for independent groups','groups':groups,'effect':{'contrast':str(arms[1])+' minus '+str(arms[0]),'risk_difference':difference,'ci95_low':max(-1,low),'ci95_high':min(1,high)},'limitations':['Observed-case analysis is not a missing-data solution or proof of a causal treatment effect.','Scientific interpretation and plan/estimand alignment need a qualified reviewer.'],'recommendation_scope':'Review work product; no clinical or regulatory decision made'}


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
