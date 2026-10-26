"""Frozen AI-authored synthetic inputs; independent labels start as null."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from .cards import FIELDS, PATTERNS

ROOT=Path(__file__).parents[2]
PATH=ROOT/"evaluation/corpora/jev-ranking-v1.json"
VERSION="autohunt-jev-ranking-20261026-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def opaque(*parts):
    return hashlib.sha256("jev-v1:".encode()+":".join(map(str,parts)).encode()).hexdigest()[:20]


def lines(text,prefix):
    return {f"{prefix}{i}":line for i,line in enumerate(text.splitlines()) if line.strip()}


def build():
    candidates=[]; jobs=[]; queries=[]; pairs=[]; design=[]
    for field in FIELDS:
        for level,(title,project,ownership) in enumerate(field["profiles"]):
            ident="c-"+opaque(field["key"],level)
            related=(1,3,5)[level]; total=(1.5,3.5,6)[level]
            education=("本科","本科","硕士")[level]
            split="test" if level==2 or field["key"] in {"llm","finance"} and level==1 else "development"
            text="\n".join([
                "【虚构候选人，仅用于离线岗位匹配评测，不代表真实求职者】",
                f"目标岗位：{field['role']}。学历：{education}。",
                f"总全职工作经验{total}年，其中{field['role']}相关经验{related}年；不把其它岗位年限折算为相关经验。",
                "明确使用的技术或业务方法："+"、".join(field["skills"])+"。未列出的技能和资格为未披露，不能推断为已掌握或不会。",
                f"项目/交付名称：{title}。",project,ownership,
                "其它项目、行业年限和资格证书未披露；所有人物、任职和项目记录均为虚构评测素材。"])
            candidate={"id":ident,"family":field["key"],"role":field["role"],"text":text,"lines":lines(text,"c"),"skills":field["skills"]}
            candidates.append(candidate); pool=[]
            for index,(kind,target,_,duty) in enumerate(PATTERNS):
                jid="j-"+opaque(ident,index)
                scene=field["scenes"][index%len(field["scenes"])]; adjacent=field["adjacent"][index%len(field["adjacent"])]
                duty=duty.format(scene=scene,adjacent=adjacent,role=field["role"],too_many=related+3)
                must=("负责明确分工内的执行、质量检查及资料记录。", "独立负责模块交付、验收和上线/业务复盘。", "牵头跨团队方案评审、风险识别与交付治理，需提供职责证据。")[level]
                needs=f"要求{field['role']}相关经验至少{related}年、学历本科及以上。"
                if kind in {"tenure","degree","missing","contradictory"}: needs="学历或年限以本段详细说明为准；未给出的限制不得补全。"
                elif kind=="negation": needs="相关经验及学历没有硬性年限或学位限制，按具体交付能力评估。"
                skill_text="主要方法或技术："+"、".join(field["skills"][(index%2):])+"。"
                if kind=="lexical": skill_text="招聘页沿用原技术标签，但实际职责按正文核对，不能因标签重合就认定适合。"
                requirements=[needs,skill_text,must,duty,"未明确标为必需的场景、工具和负责人经历属于优先条件；不推断缺失信息。"]
                # Two renderings avoid making a numbered schema the only input style.
                body=(["【虚构招聘JD，仅用于离线评测】",f"职位：{field['role']}；团队业务：{scene}。",*requirements]
                      if index%2==0 else ["【虚构招聘JD，仅用于离线评测】",f"我们在{scene}团队招聘{field['role']}。",requirements[3],requirements[0],requirements[2],requirements[1],requirements[4]])
                job_text="\n".join(body)
                jobs.append({"id":jid,"family":field["key"],"title":field["role"],"text":job_text,"lines":lines(job_text,"j")})
                pid="p-"+opaque(ident,jid)
                pairs.append({"id":pid,"candidate_id":ident,"job_id":jid,"split":split,
                              "relevance":None,"eligibility":None,"label_source":"unannotated","human_review":"pending"})
                # Not a target supplied to a judge or an evaluation truth source.
                design.append({"pair_id":pid,"case_family":kind,"intended_relevance":target,
                               "status":"author_design_intent_only_not_a_label"})
                pool.append(jid)
            random.Random(opaque(ident,"shuffle")).shuffle(pool)
            queries.append({"id":"q-"+opaque(ident),"candidate_id":ident,"split":split,"pool":pool})
    rng=random.Random(20261026); rng.shuffle(pairs); rng.shuffle(jobs)
    return {"version":VERSION,"synthetic_only":True,"human_gold":False,
            "construction":"AI-authored fictional scenario cards, deterministically assembled; no live generative model used",
            "label_status":"pending independent Jev annotation; no manufactured 0/1/2 result distribution",
            "scope":"same-occupation fixed candidate pools; shared authoring patterns, not unseen-domain evidence",
            "candidates":candidates,"jobs":jobs,"queries":queries,"pairs":pairs,"design_intents":design}


def load():
    data=json.loads(PATH.read_text(encoding="utf-8"))
    if data!=build(): raise ValueError("Corpus differs from frozen synthetic generator; create a new version instead")
    return data


def write():
    data=build()
    if PATH.exists() and json.loads(PATH.read_text(encoding="utf-8"))!=data:
        raise ValueError("Never overwrite a different frozen corpus")
    PATH.parent.mkdir(parents=True,exist_ok=True)
    PATH.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"path":str(PATH),"sha256":digest(data),"candidates":len(data["candidates"]),"pairs":len(data["pairs"]),"actual_jev_labels":0}


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--write",action="store_true"); args=parser.parse_args()
    print(json.dumps(write() if args.write else {"sha256":digest(load())},ensure_ascii=False))
