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
    data=rows(args.data,['sample_id','kind','nominal','signal','dilution'])
    unique([r['sample_id'] for r in data],'Sample IDs')
    if plan.get('calibration')!='linear_unweighted_demo':
        raise ValueError('The supplied method does not permit this bounded linear demonstration; select the approved scientific method')
    threshold=number(plan['qc_max_relative_error_percent'],'Company QC tolerance')
    if threshold<=0:raise ValueError('Company QC tolerance must be positive')
    if any(r['kind'] not in {'standard','control','unknown'} for r in data):raise ValueError('Unknown sample kind')
    standards=[r for r in data if r['kind']=='standard']
    if len(standards)<3:raise ValueError('At least three standards are needed')
    xs=[number(r['nominal'],'Standard concentration') for r in standards];ys=[number(r['signal'],'Signal') for r in standards]
    xm,ym=statistics.mean(xs),statistics.mean(ys);den=sum((x-xm)**2 for x in xs)
    if den<=0:raise ValueError('Standard concentrations must vary')
    slope=sum((x-xm)*(y-ym) for x,y in zip(xs,ys))/den;intercept=ym-slope*xm
    if slope<=0:raise ValueError('This approved demonstration requires an increasing response curve')
    total=sum((y-ym)**2 for y in ys);residual=sum((y-(intercept+slope*x))**2 for x,y in zip(xs,ys));r2=1-residual/total if total else None
    results=[];issues=[]
    for row in data:
        signal=number(row['signal'],'Signal');dilution=number(row['dilution'],'Dilution')
        if dilution<=0:raise ValueError('Dilution must be positive')
        observed=(signal-intercept)/slope;final=observed*dilution;flags=[];relative=None
        inside_low=observed>=min(xs) or math.isclose(observed,min(xs),rel_tol=1e-12,abs_tol=1e-12)
        inside_high=observed<=max(xs) or math.isclose(observed,max(xs),rel_tol=1e-12,abs_tol=1e-12)
        if not (inside_low and inside_high):flags.append('Outside calibration range; no validated extrapolation')
        if row['kind']=='control':
            nominal=number(row['nominal'],'Control nominal')
            if nominal<=0:raise ValueError('Control nominal must be positive')
            relative=100*(final-nominal)/nominal
            if abs(relative)>threshold:flags.append('Outside company-supplied QC tolerance')
        if flags:issues.append({'sample_id':row['sample_id'],'issues':flags})
        results.append({'sample_id':row['sample_id'],'kind':row['kind'],'estimated_concentration':final,'unit':plan['concentration_unit'],'relative_error_percent':relative,'review_note':'; '.join(flags)})
    save_csv(out/'assay-results.csv',results)
    return {'status':'review_required','method':plan['calibration'],'slope':slope,'intercept':intercept,'r_squared':r2,'samples':len(data),'qc_tolerance_from_company_percent':threshold,'issues':issues,'limitations':['This synthetic linear example is not an assay-validation claim.','Company method, instrument suitability and scientific interpretation need qualified review.'],'files':['assay-results.csv']}


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
