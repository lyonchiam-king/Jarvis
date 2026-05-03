from .trader import TraderSkillAdapter

AVAILABLE_SKILLS = {
    TraderSkillAdapter.NAME: TraderSkillAdapter,
}


def load_all():
    return {name: cls() for name, cls in AVAILABLE_SKILLS.items()}
