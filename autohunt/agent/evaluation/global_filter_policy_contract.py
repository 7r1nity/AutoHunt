"""Global retired-dimension projection; synthetic values, no external actions."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.core.filter_policy import (FilterPolicyRepair, POLICY_VERSION, disabled_only,
    project_arguments, project_filter, project_groups, project_keywords, project_text)
from app.core.job_filters import (evaluate_filter, filter_digest, filter_facts_digest,
    partial_filter_status, validate_filter)
from app.core.salary_conditions import SalaryPredicate, judge_salary
from app.llm.base import LLMResult


TRACES = []


def leaf(text, **extra):
    return {"op": "condition", "text": text, **extra}


def monthly(amount=12000):
    return leaf("月薪至少12000元", salary={"unit": "monthly", "minimum": amount})


class Projection(unittest.TestCase):
    def test_retired_legacy_labels_and_questions_are_removed(self):
        values = ["外包岗位", "不要外包", "非第三方岗位", "劳务派遣关系", "正式全职", "实习岗位",
            "只要应届生", "校招岗位", "社招", "三年以上项目经历", "工作经验至少3年",
            "项目经历3年以上", "N年的项目经历", "至少3年", "请确认是否外包岗位？", "实习/正式岗位", "应届/校招", "资深社招",
            "不要外包项目", "驻场外包", "实习也可以", "正式岗", "实习岗", "外包岗", "校招岗", "当前为实习岗位"]
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(project_filter(leaf(value)))
                self.assertTrue(disabled_only(value))

    def test_ordinary_skills_benefits_names_and_education_survive(self):
        for value in ["熟悉Python", "RAG项目实践", "有五险一金", "公司名称包含外包云",
                      "第三方支付经验", "硕士学历", "公司名称为实习科技", "确认",
                      "有正式上线项目经验", "正式发布流程", "人力资源专员", "深圳实习科技有限公司"]:
            with self.subTest(value=value):
                self.assertEqual(project_filter(leaf(value)), leaf(value))
                self.assertFalse(disabled_only(value))
        for value in ["外包项目管理经验","外包项目交付能力","外包项目经理","第三方库经验","第三方API开发"]:
            self.assertEqual(project_text(value),value)
        self.assertEqual(project_filter(leaf("正式科技",dimension="company")),leaf("正式科技",dimension="company"))

    def test_removing_years_preserves_technical_experience(self):
        for value, expected in [("三年Java开发经验", "Java开发经验"),
                                ("有三年Java实习经历", "有Java经验"),
                                ("5年以上Python项目经验", "Python经验")]:
            self.assertEqual(project_text(value), expected)

    def test_mixed_semantic_leaf_keeps_salary_city_and_benefit_words(self):
        for value, expected in [("非外包且深圳月薪15k以上", "深圳月薪15k以上"),
                                ("有五险一金且不要外包", "有五险一金"),
                                ("薪资多少、是否外包", "薪资多少")]:
            self.assertEqual(project_filter(leaf(value))["text"], expected)

    def test_nested_boolean_projection_preserves_nonretired_operators(self):
        value = {"op": "all", "children": [leaf("深圳"), {"op": "not", "children": [leaf("外包岗位")]},
            {"op": "any", "children": [
                {"op": "all", "children": [leaf("正式岗位"), monthly(15000)]},
                {"op": "all", "children": [leaf("实习岗位"), leaf("日薪300以上", salary={"unit":"daily", "minimum":300})]}]}]}
        original = copy.deepcopy(value)
        result = project_filter(value)
        self.assertEqual(result["children"][0], leaf("深圳"))
        self.assertEqual(result["children"][1]["op"], "any")
        self.assertEqual([x["salary"]["minimum"] for x in result["children"][1]["children"]], [15000, 300])
        self.assertEqual(value, original)
        self.assertEqual(project_filter(result), result)

    def test_not_of_retained_predicate_stays_not(self):
        result = project_filter({"op": "not", "children": [{"op": "all", "children": [leaf("外包"), leaf("倒班")]}]})
        self.assertEqual(result, {"op": "not", "children": [leaf("倒班")]})

    def test_all_retired_alternatives_become_unrestricted(self):
        self.assertIsNone(project_filter({"op": "any", "children": [leaf("实习"), leaf("正式")]}))

    def test_complex_mixed_condition_is_internal_repair_not_erased(self):
        value = leaf("如果是实习则日薪至少300，否则月薪至少15000")
        with self.assertRaises(FilterPolicyRepair):
            project_filter(value)
        self.assertIn("300", value["text"])
        self.assertIn("15000", value["text"])

    def test_invalid_structure_is_left_for_structural_validator(self):
        for value in [{"op": "condition", "text": "外包", "children": [leaf("深圳")]},
                      {"op": "all", "children": []}, {"op": "not", "children": [leaf("外包"), monthly()]},
                      {"op": "condition", "text": ""}, {"op": "unsupported", "text": "外包"},
                      {"op":"condition","text":42}, {"op":"condition","text":"外包","extra":"invalid"},
                      {"op":"condition","text":"实习","children":None},
                      {"op":"all","mode":"allow","children":[leaf("外包"),leaf("深圳")]}]:
            self.assertEqual(project_filter(value), value)
            with self.assertRaises(ValueError):
                validate_filter(project_filter(value))

    def test_dimension_annotation_cannot_delete_an_unrelated_condition(self):
        with self.assertRaises(FilterPolicyRepair):
            project_filter(leaf("深圳", dimension="employment_kind"))
        self.assertIsNone(project_filter(leaf("至少3年", dimension="experience_years")))
        result = project_filter(leaf("深圳正式岗位", dimension="employment_kind"))
        self.assertEqual(result, leaf("深圳"))

    def test_typed_salary_compound_preserves_city_and_numbered_skills(self):
        result = project_filter(leaf("熟悉Python3且深圳正式岗月薪15k以上",
            salary={"unit":"monthly","minimum":15000}))
        self.assertEqual(result["op"], "all")
        self.assertEqual(result["children"][0]["salary"]["minimum"], 15000)
        self.assertEqual(result["children"][1]["text"], "熟悉Python3且深圳")
        self.assertEqual(project_filter(result), result)

    def test_typed_salary_keeps_units_and_range_without_type_caption(self):
        result = project_filter(leaf("仅限制实习岗位：日薪区间下限≥300元",
            salary={"unit":"daily","minimum":300,"applies_to":"internship"}))
        self.assertEqual(result["text"], "日薪区间下限≥300元")
        self.assertEqual(result["salary"], {"unit":"daily","minimum":300,"applies_to":"all"})
        self.assertEqual(result["op"], "condition")

    def test_typed_salary_caption_does_not_become_an_extra_semantic_requirement(self):
        result=project_filter(leaf("合成薪资条件",salary={"unit":"monthly","minimum":12000}))
        self.assertEqual(result["op"],"condition")
        self.assertEqual(result["salary"]["minimum"],12000)

    def test_unrelated_typed_salary_is_byte_shape_stable_for_action_binding(self):
        for value in [leaf("月薪至少15k",salary={"unit":"monthly","minimum":15000}),
                      leaf("合成薪资条件",salary={"unit":"daily","minimum":300,"applies_to":"all"}),
                      {"op":"condition","salary":{"unit":"monthly","minimum":15000}}]:
            original=json.dumps(value,ensure_ascii=False,sort_keys=True)
            self.assertEqual(json.dumps(project_filter(value),ensure_ascii=False,sort_keys=True),original)

    def test_unrelated_semantic_shape_and_whitespace_are_stable_for_action_binding(self):
        for value in [leaf("  深圳  "), {"op":"all","label":"  保留分组说明  ","children":[leaf("有五险一金")]},
                      {"op":"any","children":[leaf("深圳"),leaf("上海")]}]:
            self.assertEqual(project_filter(value),value)

    def test_absent_dimension_keeps_legacy_serialization_fingerprint(self):
        normalized = validate_filter(leaf("深圳")).model_dump(mode="json")
        self.assertNotIn("dimension", normalized)
        self.assertEqual(filter_digest(validate_filter(project_filter(normalized))), filter_digest(validate_filter(normalized)))

    def test_category_groups_removed_with_total_preserved(self):
        groups = [{"label": "正式岗位", "count": 10, "filters": leaf("正式岗位")},
                  {"label": "实习岗位", "count": 5, "filters": leaf("实习岗位")}]
        self.assertEqual(project_groups(groups), (None, 15))
        result = project_arguments({"result_groups": groups, "result_limit": 99})
        self.assertEqual(result, {"result_groups": None, "result_limit": 15})

    def test_mixed_location_and_category_quotas_keep_location_and_open_quota(self):
        groups, total = project_groups([{"label":"深圳正式", "count":3,
            "filters":{"op":"all", "children":[leaf("深圳"),leaf("正式")]}},
            {"label":"实习", "count":2, "filters":leaf("实习")}])
        self.assertEqual(total, 5)
        self.assertEqual(groups[0]["filters"], leaf("深圳"))
        self.assertIsNone(groups[1]["filters"])

    def test_projected_duplicate_groups_merge_counts_without_losing_constraints(self):
        groups,total = project_groups([{"label":"深圳正式","count":3,"filters":leaf("深圳正式")},
            {"label":"深圳实习","count":2,"filters":leaf("深圳实习")}])
        self.assertEqual(total,5)
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]["count"],5)
        self.assertEqual(groups[0]["filters"],leaf("深圳"))

    def test_retired_groups_do_not_hide_invalid_counts_or_original_duplicate_labels(self):
        base={"label":"实习","count":1,"filters":leaf("实习")}
        for groups in [[{**base,"count":0}], [{**base,"count":101}], [{**base,"count":True}],
                       [{**base,"label":str(i)} for i in range(11)],
                       [{**base,"count":60}, {**base,"label":"正式","count":60}],
                       [base, {**base,"filters":leaf("深圳")}],
                       [{**base,"extra":"invalid"}]]:
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                project_groups(groups)

    def test_same_projected_label_for_different_groups_gets_stable_suffix(self):
        groups,total = project_groups([{"label":"同名正式","count":3,"filters":leaf("深圳")},
            {"label":"同名实习","count":2,"filters":leaf("上海")}])
        self.assertEqual(total,5)
        self.assertNotEqual(groups[0]["label"],groups[1]["label"])
        self.assertEqual([x["filters"]["text"] for x in groups],["深圳","上海"])

    def test_unrelated_grouping_keeps_labels_and_existing_action_shape(self):
        groups=[{"label":"方案A","count":3,"filters":leaf("深圳")},
                {"label":"方案B","count":2,"filters":leaf("深圳")}]
        self.assertEqual(project_groups(groups),(groups,5))
        self.assertEqual(project_arguments({"result_groups":groups}),{"result_groups":groups})

    def test_keywords_and_request_fields_use_one_projection(self):
        self.assertEqual(project_keywords(["外包", "实习", "华为", "Python", "AI实习开发工程师"]),
                         ["华为", "Python", "AI开发工程师"])
        original = {"filters": {"op":"not","children":[leaf("外包")]}, "queries":["AI实习开发工程师"],
                    "excluded_keywords":["实习","Java"], "salary_min":12000, "locations":["深圳"], "limit":20}
        result = project_arguments(original)
        self.assertIsNone(result["filters"])
        self.assertEqual(result["queries"], ["AI开发工程师"])
        self.assertEqual(result["excluded_keywords"], ["Java"])
        self.assertEqual((result["salary_min"], result["locations"], result["limit"]), (12000,["深圳"],20))
        self.assertEqual(original["excluded_keywords"], ["实习","Java"])

    def test_invalid_keyword_types_are_not_coerced_to_new_search_terms(self):
        with self.assertRaises(ValueError):
            project_keywords([42])


class Evaluation(unittest.IsolatedAsyncioTestCase):
    async def test_retired_only_filter_never_calls_model_and_passes(self):
        async def forbidden(_):
            raise AssertionError("Retired conditions must never reach a model")
        result = await evaluate_filter(forbidden, {"op":"not", "children":[leaf("外包")]}, {})
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["conditions"], [])
        self.assertEqual(result["scope_policy_version"], POLICY_VERSION)

    async def test_old_unknown_type_evidence_does_not_hide_known_salary_and_city(self):
        facts={"title":"合成工程师","company":"合成公司","location":"深圳","salary":"19-23K","description":"工作地点深圳"}
        tree={"op":"all","children":[leaf("深圳"),leaf("外包"),monthly(),leaf("实习")]}
        prior={"policy_version":3,"facts_digest":filter_facts_digest(facts), "conditions":[
            {"text":"深圳","status":"pass","mode":"require","reason":"原文明确","evidence":[{"field":"location","quote":"深圳"}]},
            {"text":"外包","status":"unknown","evidence":[]},
            {"text":"实习","status":"unknown","evidence":[]}]}
        async def forbidden(_): raise AssertionError("Known unchanged city/salary can be reused")
        result=await evaluate_filter(forbidden,tree,facts,prior_evidence=prior)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(result["conditions"]), 2)
        self.assertTrue(result["conditions"][0]["reused"])
        TRACES.append({"case":"old_unknown_projection","result":result})

    async def test_retained_salary_failure_still_fails_without_model(self):
        async def forbidden(_): raise AssertionError("Typed salary needs no model")
        result=await evaluate_filter(forbidden,{"op":"all","children":[monthly(),leaf("实习")]},{"salary":"8-10K"})
        self.assertEqual(result["status"], "fail")
        self.assertEqual(len(result["conditions"]), 1)

    async def test_retained_benefits_unknown_does_not_become_pass(self):
        async def response(_):
            return LLMResult('{"conditions":[{"index":0,"status":"unknown","reason":"未披露福利","evidence":[]}]}',"scripted","offline")
        result=await evaluate_filter(response,{"op":"all","children":[leaf("实习"),leaf("有五险一金")]},{})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["conditions"][0]["text"], "有五险一金")

    async def test_early_salary_screen_ignores_retired_dimensions(self):
        tree={"op":"all","children":[monthly(),leaf("应届"),leaf("正式")]}
        self.assertEqual(partial_filter_status(tree,{"salary":"19-23K"}), "pass")
        self.assertEqual(partial_filter_status(tree,{"salary":"8-10K"}), "fail")

    async def test_salary_applicability_no_longer_needs_employment_classification(self):
        salary=SalaryPredicate(unit="daily",minimum=300,applies_to="internship")
        self.assertEqual(salary.applies_to,"all")
        self.assertEqual(judge_salary(salary,{"salary":"500-600元/天"})["status"],"pass")
        self.assertEqual(judge_salary(salary,{"salary":"150-200元/天","employment_kind":"regular"})["status"],"fail")
        self.assertEqual(judge_salary(salary,{"salary":"15-20K","employment_kind":"regular"})["status"],"fail")


def main():
    import sys
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    output=Path('evaluation/results/global-filter-policy')/datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')
    output.mkdir(parents=True)
    summary={"tests":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),
             "external_model_calls":0,"real_recruitment_actions":0}
    (output/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    (output/'traces.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in TRACES),encoding='utf-8')
    print(json.dumps({"output":str(output),**summary}));return int(not result.wasSuccessful())


if __name__=='__main__': raise SystemExit(main())
