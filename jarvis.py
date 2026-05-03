"""Jarvis - Personal Assistant."""

from skills import load_all


class Jarvis:
    def __init__(self):
        self.skills = load_all()

    def ask(self, skill_name: str, command: str, **kwargs) -> str:
        if skill_name not in self.skills:
            available = ", ".join(self.skills.keys()) or "none"
            return f"Skill '{skill_name}' not found. Available: {available}"
        return self.skills[skill_name].handle(command, **kwargs)


if __name__ == "__main__":
    jarvis = Jarvis()

    result = jarvis.ask("trader", "analyze", ticker="BRK.B", metrics={
        "price": 350.0,
        "eps": 14.0,
        "book_value": 220.0,
        "roe": 0.19,
        "debt_equity": 0.2,
        "revenue_growth": 0.07,
    })
    print(result)
    print()

    result = jarvis.ask("trader", "philosophy")
    print(result)
