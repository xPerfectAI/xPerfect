"""Original xPerfect aid: explicit volume/mix/price convention, no hidden plug."""
from decimal import Decimal


def values(items):
    result=[Decimal(str(x)) for x in items]
    if not result or any(not x.is_finite() or x<0 for x in result):
        raise ValueError('Use nonnegative finite values under the approved quantity/price convention')
    return result


def percentage(change,base):
    change,base=Decimal(str(change)),Decimal(str(base))
    if not change.is_finite() or not base.is_finite():raise ValueError('Inputs must be finite')
    return None if base==0 else change/abs(base)*100


def reconcile(start,drivers,end,tolerance='0.00000001'):
    start,end=Decimal(str(start)),Decimal(str(end));drivers=[Decimal(str(x)) for x in drivers];tol=Decimal(str(tolerance))
    if tol<0 or any(not x.is_finite() for x in [start,end,tol,*drivers]):raise ValueError('Finite evidence and nonnegative tolerance required')
    residual=end-start-sum(drivers)
    return {'reconciled':abs(residual)<=tol,'unexplained_residual':residual}


def bridge(budget_quantity,actual_quantity,budget_price,actual_price):
    bq,aq,bp,ap=map(values,[budget_quantity,actual_quantity,budget_price,actual_price])
    if len({len(x) for x in [bq,aq,bp,ap]})!=1:raise ValueError('Product vectors must align')
    qb,qa=sum(bq),sum(aq);budget=sum(q*p for q,p in zip(bq,bp));actual=sum(q*p for q,p in zip(aq,ap));variance=actual-budget
    if qb==0 or qa==0:
        return {'status':'needs_method','reason':'Mix is undefined with zero total volume','budget':budget,'actual':actual,'variance':variance}
    mb,ma=[x/qb for x in bq],[x/qa for x in aq]
    volume=(qa-qb)*sum(m*p for m,p in zip(mb,bp))
    mix=qa*sum((a-b)*p for a,b,p in zip(ma,mb,bp))
    price=qa*sum(m*(a-b) for m,a,b in zip(ma,ap,bp))
    return {'status':'calculated','order':'volume, mix, price','budget':budget,'actual':actual,'variance':variance,'volume':volume,'mix':mix,'price':price,**reconcile(budget,[volume,mix,price],actual)}
