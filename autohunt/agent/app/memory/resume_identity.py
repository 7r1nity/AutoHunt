"""Stable version identity shared by resume-bound Agent actions."""


def resume_stamp(document, candidate) -> str:
    return f"{candidate.read().profile.resume_id}:{document.id}:{document.version}"
