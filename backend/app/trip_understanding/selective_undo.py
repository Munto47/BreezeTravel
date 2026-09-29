"""Reverse a recorded change without replacing unrelated later values."""
from copy import deepcopy
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.models import UserFacingTripResult

_MISSING=object()
_FIELDS=('days','assumptions','lodging_constraints','pending_lodgings','issue_dispositions','source_restorations',
         'visit_decisions','correction_suggestions')


def _normalized(result):
    value=result.model_dump(mode='json')
    tokens={}
    for day in value['days']:
        for card in day['activities']:
            tokens[card['activity_token']]=card['visit_id']
        for alt in day['alternatives']:
            if alt['activity_token']:tokens[alt['activity_token']]=alt['alternative_id']
    for card in value['lodging_constraints']:tokens[card['activity_token']]=card['visit_id']
    for hotel in value['pending_lodgings']:tokens[hotel['pending_token']]=hotel['lodging_id']
    def clean(value):
        if isinstance(value,dict):return {key:clean(item) for key,item in value.items()}
        if isinstance(value,list):return [clean(item) for item in value]
        return tokens.get(value,value) if isinstance(value,str) else value
    return clean({key:value[key] for key in _FIELDS})


def _reverse(before, after, current):
    if before==after:return deepcopy(current)
    if current==after:return deepcopy(before)
    if all(isinstance(value,dict) for value in (before,after,current)):
        result=deepcopy(current)
        for key in before.keys() | after.keys():
            old,new,now=before.get(key,_MISSING),after.get(key,_MISSING),current.get(key,_MISSING)
            if old==new:continue
            if now is _MISSING and new is _MISSING:
                result[key]=deepcopy(old)
            elif old is _MISSING and now==new:
                result.pop(key,None)
            elif _MISSING in (old,new,now):
                raise CommandTargetChangedError('later edit conflicts with this change')
            else:result[key]=_reverse(old,new,now)
        return result
    if all(isinstance(value,list) for value in (before,after,current)):
        identity=next((key for key in ('day_id','visit_id','note_id','alternative_id','lodging_id','suggestion_id','key')
            if all(isinstance(item,dict) and key in item for group in (before,after,current) for item in group)),None)
        if identity:
            old={item[identity]:item for item in before};new={item[identity]:item for item in after};now={item[identity]:item for item in current}
            merged=_reverse(old,new,now)
            # Ordering changes are guarded separately from object fields.
            order=_reverse([item[identity] for item in before],[item[identity] for item in after],[item[identity] for item in current])
            return [merged[key] for key in order]
    raise CommandTargetChangedError('later edit conflicts with this change')


def reverse_change(current, before, after):
    payload=current.model_dump(mode='json')
    payload.update(_reverse(_normalized(before),_normalized(after),_normalized(current)))
    current_cards={card.visit_id:card for day in current.days for card in day.activities}
    current_cards.update({card.visit_id:card for card in current.lodging_constraints})
    before_cards={card.visit_id:card for day in before.days for card in day.activities}
    before_cards.update({card.visit_id:card for card in before.lodging_constraints})
    after_cards={card.visit_id:card for day in after.days for card in day.activities}
    after_cards.update({card.visit_id:card for card in after.lodging_constraints})
    tokens={}
    for card in [card for day in payload['days'] for card in day['activities']]+payload['lodging_constraints']:
        ref=card['visit_id'];old=before_cards.get(ref);new=after_cards.get(ref)
        identity_changed=old and new and any(getattr(old,key)!=getattr(new,key) for key in ('name','city','status','area_or_address','category','photo_url'))
        basis=old if identity_changed or ref not in current_cards else current_cards[ref]
        if basis:tokens[ref]=basis.activity_token
    for result in (before,current):
        for day in result.days:
            for item in day.alternatives:
                if item.activity_token:tokens[item.alternative_id]=item.activity_token
        for item in result.pending_lodgings:tokens[item.lodging_id]=item.pending_token
    def restore(value, key=''):
        if isinstance(value,dict):return {name:restore(item,name) for name,item in value.items()}
        if isinstance(value,list):return [restore(item,key) for item in value]
        return tokens.get(value,value) if isinstance(value,str) and ('token' in key) else value
    for key in _FIELDS:payload[key]=restore(payload[key])
    try:return UserFacingTripResult.model_validate(payload)
    except ValueError as exc:raise CommandTargetChangedError('change conflicts with current arrangement') from exc
