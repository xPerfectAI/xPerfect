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
    data=rows(args.data,['claim_id','statement','source_file','source_section','source_version','owner'])
    unique([r['claim_id'] for r in data],'Claim IDs')
    base=Path(args.plan).resolve().parent;documents=plan['documents']
    if not isinstance(documents,list):raise ValueError('The evidence index must list documents')
    by_path={x['path']:x for x in documents};issues=[];mapped=[]
    for row in data:
        rel=Path(row['source_file']);candidate=(base/rel).resolve()
        if rel.is_absolute() or not candidate.is_relative_to(base):raise ValueError('Evidence references must stay within the supplied materials folder')
        problems=[];entry=by_path.get(row['source_file']);digest=None
        if not entry or not candidate.is_file():problems.append('Referenced evidence is unavailable')
        else:
            content=candidate.read_text(encoding='utf-8');digest=hashlib.sha256(content.encode()).hexdigest()
            if str(entry['version'])!=row['source_version']:problems.append('Referenced version does not match supplied document')
            if entry.get('review_status')!='approved':problems.append('Document approval is not established')
            if not row['source_section'].strip():problems.append('Source section is not specified')
            elif row['source_section'] not in entry.get('sections',[]):problems.append('Declared section not found in supplied document index')
        if not row['owner'].strip():problems.append('Responsible reviewer not specified')
        if problems:issues.append({'claim_id':row['claim_id'],'issues':problems})
        mapped.append({'claim_id':row['claim_id'],'statement':row['statement'],'source_file':row['source_file'],'source_version':row['source_version'],'sha256':digest,'review_note':'; '.join(problems) or 'Mechanical reference checks complete; scientific support still needs review'})
    save_csv(out/'evidence-map.csv',mapped)
    return {'status':'review_required','claims':len(data),'issues':issues,'limitations':['Mechanical reference integrity does not prove that a statement is scientifically or legally supported.','Read original documents and apply the actual product, jurisdiction, intended use and company process.'],'files':['evidence-map.csv'],'submission_or_approval_performed':False}


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
