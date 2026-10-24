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
    data=rows(args.data,['month','account','type','currency','actual','budget'])
    unique([(r['month'],r['account']) for r in data],'Month/account pairs');results=[];months={}
    for row in data:
        if row['type'] not in {'revenue','expense'}:raise ValueError('Map the account to revenue or expense explicitly before using this management-reporting aid')
        if row['currency']!=plan['currency']:raise ValueError('Mixed currencies require an approved conversion policy')
        actual,budget=number(row['actual'],'Actual'),number(row['budget'],'Budget')
        if min(actual,budget)<0:raise ValueError('This input contract uses positive revenue/expense measures; reconcile sign conventions first')
        m=months.setdefault(row['month'],{'month':row['month'],'revenue':0.,'expenses':0.,'budget_revenue':0.,'budget_expenses':0.})
        kind='revenue' if row['type']=='revenue' else 'expenses';m[kind]+=actual;m['budget_'+kind]+=budget
        results.append({'month':row['month'],'account':row['account'],'actual':actual,'budget':budget,'variance':actual-budget,'favorable_variance':actual-budget if row['type']=='revenue' else budget-actual})
    for row in months.values():row['operating_result']=row['revenue']-row['expenses'];row['budget_operating_result']=row['budget_revenue']-row['budget_expenses']
    cash=number(plan['opening_cash'],'Opening cash');forecast=[]
    for row in plan['cash_plan']:
        receipts,payments=number(row['receipts'],'Forecast receipts'),number(row['payments'],'Forecast payments')
        if min(receipts,payments)<0:raise ValueError('Forecast receipts/payments must be nonnegative')
        opening=cash;cash+=receipts-payments;forecast.append({'month':row['month'],'opening_cash':opening,'receipts':receipts,'payments':payments,'closing_cash':cash})
    bank,ledger=number(plan['bank_cash'],'Bank cash'),number(plan['ledger_cash'],'Ledger cash');difference=bank-ledger
    save_csv(out/'account-variances.csv',results);save_csv(out/'cash-forecast.csv',forecast)
    return {'status':'review_required','currency':plan['currency'],'monthly_results':list(months.values()),'cash_reconciliation':{'bank':bank,'ledger':ledger,'unexplained_difference':difference,'reconciled':difference==0},'cash_forecast':forecast,'limitations':['Forecast values are supplied scenarios, not predictions or an audited financial statement.','No ledger posting, transfer, investment or tax decision was made.'],'files':['account-variances.csv','cash-forecast.csv']}


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
