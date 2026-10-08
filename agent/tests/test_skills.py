from pathlib import Path

from app.memory.stores import SkillStore
from app.sandbox_backend.filesystem import WorkspaceSandbox


def test_skill_proposal_requires_separate_approval(tmp_path: Path) -> None:
    store = SkillStore(WorkspaceSandbox(tmp_path))
    proposal = store.propose("job-search", "Use confirmed sources.", "Improve source policy")
    assert store.list() == []
    assert len(store.list_proposals()) == 1

    store.approve_proposal(proposal["id"])
    assert store.list() == ["job-search"]
    assert store.list_proposals() == []
