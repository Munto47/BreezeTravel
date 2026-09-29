"""Owner-readable changes over existing immutable revision records."""
import json

_LABELS={'ACTIVITY_MOVE':'调整地点顺序','ACTIVITY_TEXT_EDIT':'修改地点文案','PLACE_CONFIRM':'确认或更换地点',
    'ACTIVITY_INSERT':'新增地点','ACTIVITY_DELETE':'移除地点','SOURCE_RESTORE':'恢复原文内容',
    'ISSUE_DISPOSITION':'处理检查事项','DINING_INSERT':'加入用餐安排','LODGING_RECOVER':'恢复住宿安排',
    'ALTERNATIVE_INSERT':'加入原文备选','CHOICE_SELECT':'选择原文方案','CHOICE_CLEAR':'撤销方案选择',
    'PLACE_REPLACE':'更换地点'}


async def read_changes(repository, resource):
    if hasattr(repository,'_get_pool'):
        pool=await repository._get_pool()
        rows=await pool.fetch('''SELECT v.opaque_etag,r.proposal_json FROM trip_understanding_revisions r
            JOIN trip_understanding_results v ON v.understanding_id=r.understanding_id AND v.revision=r.revision
            WHERE r.understanding_id=$1 AND r.proposal_json->>'kind'='USER_EDIT'
            ORDER BY r.revision DESC LIMIT 20''',resource.understanding_id)
        rows=[{'change_etag':row['opaque_etag'],'command':(json.loads(row['proposal_json']) if isinstance(row['proposal_json'],str) else row['proposal_json']).get('command_type')} for row in rows]
    else:
        records=sorted(((repository.result_revisions[key],value) for key,value in repository.results.items()
            if repository.result_owners.get(key)==resource.understanding_id),reverse=True,key=lambda row:row[0])
        rows=[{'change_etag':value.opaque_etag,'command':repository.g03_pipeline_inputs.get((resource.understanding_id,revision),{}).get('command_type')}
            for revision,value in records[:20]]
    return [{'change_etag':row['change_etag'],'title':_LABELS.get(row['command'],'修改行程')}
        for row in rows if row['command'] and row['command'] not in {'UNDO','REDO'}]
