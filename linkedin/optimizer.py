import json
import anthropic


def _build_prompt(profile: dict) -> str:
    experience_text = ""
    for i, exp in enumerate(profile.get("experience", []), 1):
        experience_text += f"\n  Role {i}:\n"
        experience_text += f"    Title: {exp.get('title', '')}\n"
        experience_text += f"    Company: {exp.get('company', '')}\n"
        experience_text += f"    Duration: {exp.get('duration', '')}\n"
        bullets = exp.get("bullets", [])
        if bullets:
            experience_text += "    Current bullets:\n"
            for b in bullets:
                experience_text += f"      - {b}\n"

    skills_text = ", ".join(profile.get("skills", []))

    return f"""You are an expert LinkedIn profile optimizer. Rewrite the following LinkedIn profile to maximize recruiter visibility, keyword density, and professional impact.

--- CURRENT PROFILE ---
Headline: {profile.get('headline', '(none)')}

About:
{profile.get('about', '(none)')}

Experience:{experience_text if experience_text else ' (none)'}

Skills: {skills_text if skills_text else '(none)'}
--- END CURRENT PROFILE ---

Rewrite rules:
1. HEADLINE: "[Role] | [Value Proposition] | [Who You Help]". Under 220 characters. Pack in keywords naturally.
2. ABOUT: Hook opening line, first person, 3-5 paragraphs, results-focused with metrics, soft CTA at the end. 200-300 words.
3. EXPERIENCE: 3-5 bullets per role. Start with action verb, lead with metric where possible, one line each.
4. SKILLS: Exactly 12-15 highly searchable LinkedIn skill keywords. Return as JSON array.

Return a single valid JSON object — no markdown fences, no commentary:
{{
  "headline": "<rewritten headline>",
  "about": "<rewritten about as plain text with newlines>",
  "experience": [
    {{
      "title": "<copy exactly from input>",
      "company": "<copy exactly from input>",
      "duration": "<copy exactly from input>",
      "bullets": ["<bullet 1>", "<bullet 2>", ...]
    }}
  ],
  "skills": ["<skill 1>", "<skill 2>", ...]
}}"""


def optimize_profile(profile: dict, api_key: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": _build_prompt(profile)}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    optimized = json.loads(raw)
    for key in ("headline", "about", "experience", "skills"):
        if key not in optimized:
            raise ValueError(f"Claude response missing key: '{key}'")
    return optimized
