"""Static catalogs shared by the API and the UI."""
import re
import zlib

# Gemini TTS prebuilt voices: their published character, and whether the voice
# sounds female or male (Google's Chirp 3 HD table lists the same 30 voices).
# Hosts are cast by gender first, so a "Dr. Elena" never speaks with a male voice.
VOICE_INFO: dict[str, tuple[str, str]] = {
    "Zephyr": ("Bright", "female"), "Puck": ("Upbeat", "male"), "Charon": ("Informative", "male"),
    "Kore": ("Firm", "female"), "Fenrir": ("Excitable", "male"), "Leda": ("Youthful", "female"),
    "Orus": ("Firm", "male"), "Aoede": ("Breezy", "female"), "Callirrhoe": ("Easy-going", "female"),
    "Autonoe": ("Bright", "female"), "Enceladus": ("Breathy", "male"), "Iapetus": ("Clear", "male"),
    "Umbriel": ("Easy-going", "male"), "Algieba": ("Smooth", "male"), "Despina": ("Smooth", "female"),
    "Erinome": ("Clear", "female"), "Algenib": ("Gravelly", "male"), "Rasalgethi": ("Informative", "male"),
    "Laomedeia": ("Upbeat", "female"), "Achernar": ("Soft", "female"), "Alnilam": ("Firm", "male"),
    "Schedar": ("Even", "male"), "Gacrux": ("Mature", "female"), "Pulcherrima": ("Forward", "female"),
    "Achird": ("Friendly", "male"), "Zubenelgenubi": ("Casual", "male"), "Vindemiatrix": ("Gentle", "female"),
    "Sadachbia": ("Lively", "male"), "Sadaltager": ("Knowledgeable", "male"), "Sulafat": ("Warm", "female"),
}
VOICES: dict[str, str] = {name: character for name, (character, _) in VOICE_INFO.items()}
VOICE_GENDER: dict[str, str] = {name: gender for name, (_, gender) in VOICE_INFO.items()}
GENDERS = ("female", "male")

# When a host needs a voice (or theirs doesn't fit), these are tried in order.
VOICE_PREFERENCE = {
    "female": ["Kore", "Aoede", "Leda", "Sulafat", "Erinome", "Zephyr", "Despina", "Laomedeia",
               "Gacrux", "Achernar", "Autonoe", "Callirrhoe", "Pulcherrima", "Vindemiatrix"],
    "male": ["Puck", "Charon", "Orus", "Achird", "Algieba", "Sadaltager", "Fenrir", "Iapetus",
             "Umbriel", "Schedar", "Rasalgethi", "Enceladus", "Algenib", "Alnilam", "Sadachbia", "Zubenelgenubi"],
}


def pick_voice(gender: str, avoid: set[str] = frozenset(), like: str = "") -> str:
    """A voice of `gender` not in `avoid`, preferring one with the same character as
    `like` (so swapping a mismatched voice keeps the intended feel)."""
    options = [v for v in VOICE_PREFERENCE.get(gender) or VOICE_PREFERENCE["female"] if v not in avoid]
    character = VOICES.get(like)
    same = [v for v in options if VOICES[v] == character]
    return (same or options or [VOICE_PREFERENCE["female"][0]])[0]


# Inline vocal events the TTS model understands, limited to the ones that
# sound natural in a talk show.
VOCAL_TAGS = [
    "<laugh>", "<chuckle>", "<giggle>", "<gasp>", "<sigh>", "<groan>",
    "<snort>", "<scream>", "<whispers>", "<breath>", "<tsk>", "<cough>",
    "<short pause>", "<long pause>",
]

# Each preset carries the episode accent the UI is tinted with.
STYLE_PRESETS = [
    {
        "id": "gossip",
        "label": "Girl gossip",
        "accent": "#B8336A",
        "prompt": "Two best friends spilling the tea like it's the juiciest group chat ever: gasps, "
        "'wait, WHAT', 'no because', dramatic pauses, side-eye, finishing each other's sentences. "
        "Every fact stays accurate; only the delivery is messy.",
    },
    {
        "id": "late-night",
        "label": "Late-night radio",
        "accent": "#3B3F9E",
        "prompt": "Smooth late-night FM radio. Low, unhurried, intimate. Long thoughtful pauses, "
        "warm asides to the listeners still awake out there.",
    },
    {
        "id": "sports",
        "label": "Sports play-by-play",
        "accent": "#B5521C",
        "prompt": "Live play-by-play and color commentary. Treat the topic as a match happening right "
        "now: momentum swings, replays of key moments, stats, rising crowd energy.",
    },
    {
        "id": "true-crime",
        "label": "True crime",
        "accent": "#3A4250",
        "prompt": "Hushed true-crime podcast. Slow reveals, suspicious details, cliffhangers before "
        "each new beat, one host skeptical and one host spooked.",
    },
    {
        "id": "documentary",
        "label": "Nature documentary",
        "accent": "#1F7F6A",
        "prompt": "A reverent nature-documentary narrator and an excitable field correspondent, "
        "observing the subject like rare wildlife in its natural habitat.",
    },
    {
        "id": "professors",
        "label": "Rival professors",
        "accent": "#80620F",
        "prompt": "Two rival professors who respect each other and agree on nothing. Erudite, dry, "
        "very funny, constantly correcting each other's footnotes.",
    },
    {
        "id": "bedtime",
        "label": "Bedtime story",
        "accent": "#6A4FB0",
        "prompt": "Gentle bedtime storytelling for winding down. Soft, slow, cozy, simple words, "
        "no surprises that would wake anyone up.",
    },
    {
        "id": "morning-zoo",
        "label": "Morning radio chaos",
        "accent": "#2E6F9E",
        "prompt": "A chaotic morning-radio duo: running gags, terrible puns, mock outrage, "
        "competitive banter, way too much coffee.",
    },
]

PRESETS_BY_ID = {p["id"]: p for p in STYLE_PRESETS}

# Ready-made formats: a style plus hosts with personas and voices, and a length.
# Named after the format, not after real shows or people, so published pods
# never pass themselves off as someone else's podcast.
TEMPLATES = [
    {
        "id": "science-deep-dive",
        "label": "Science deep-dive",
        "blurb": "One expert host, long-form: how it works, what the studies say, what to do.",
        "accent": "#1F7F6A",
        "minutes": 15,
        "style": "A long-form science podcast from a scientist-educator. Calm, precise and genuinely "
        "excited about mechanisms. Explains how things work at the level of biology and evidence, says "
        "what kind of study supports each claim and where the evidence is thin, recaps with 'so, to pause "
        "on that', and closes each section with practical, low-risk protocols. Never gives personal "
        "medical advice.",
        "hosts": [
            {"name": "Dr. Elena Vance", "voice": "Erinome", "gender": "female",
             "persona": "A neuroscientist and educator who loves mechanisms, cites the evidence behind every "
             "claim, and always ends with practical protocols; calm, precise, generous with caveats."},
        ],
    },
    {
        "id": "couple-chat",
        "label": "Married couple chat",
        "blurb": "A husband and wife riffing on the topic, their life and each other.",
        "accent": "#B5521C",
        "minutes": 15,
        "style": "A warm husband-and-wife podcast recorded at the kitchen table. Inside jokes, gentle "
        "teasing, finishing each other's sentences, real-life stories from their marriage that tie back to "
        "the topic, one of them always going off on tangents and the other reeling them back in.",
        "hosts": [
            {"name": "Mia", "voice": "Aoede", "gender": "female",
             "persona": "The wife: quick, playful, loves a dramatic retelling, teases her husband but always "
             "has his back."},
            {"name": "Jordan", "voice": "Algieba", "gender": "male",
             "persona": "The husband: dry humor, goes on tangents, secretly the more romantic one."},
        ],
    },
    {
        "id": "inspirational",
        "label": "Inspirational talk",
        "blurb": "A keynote that starts with a story and builds to a big finish.",
        "accent": "#80620F",
        "minutes": 10,
        "style": "An inspirational keynote talk. Opens with a vivid personal story, uses short punchy lines "
        "and memorable metaphors, pauses for effect, builds energy section by section, and ends with a "
        "clear, uplifting call to action. Earnest, never cheesy.",
        "hosts": [
            {"name": "Grace Okafor", "voice": "Sulafat", "gender": "female",
             "persona": "A motivational speaker who overcame real setbacks; warm, grounded, builds to "
             "powerful crescendos."},
        ],
    },
    {
        "id": "girl-talk",
        "label": "Girl talk",
        "blurb": "Two best friends, real talk: candid, supportive, a little messy.",
        "accent": "#B8336A",
        "minutes": 15,
        "style": "Two best friends having real talk on the couch: candid, funny, supportive, honest about "
        "feelings, hyping each other up, gasping at the juicy parts, and always landing on practical "
        "advice a friend would actually give.",
        "hosts": [
            {"name": "Kayla", "voice": "Leda", "gender": "female",
             "persona": "The hype woman: big reactions, lives for the drama, fiercely loyal."},
            {"name": "Nia", "voice": "Laomedeia", "gender": "female",
             "persona": "The voice of reason: sharp, practical, gives the advice you need to hear."},
        ],
    },
    {
        "id": "mens-talk",
        "label": "Masculine talk",
        "blurb": "Two guys on discipline, purpose, health and brotherhood.",
        "accent": "#3A4250",
        "minutes": 15,
        "style": "Two guys talking straight about discipline, purpose, health, relationships and "
        "brotherhood. Direct, grounded, no fluff, gym-and-garage energy, honest about struggles, "
        "respectful of everyone, and always ending with concrete takeaways.",
        "hosts": [
            {"name": "Marcus", "voice": "Orus", "gender": "male",
             "persona": "A former athlete turned coach: blunt, disciplined, big on accountability."},
            {"name": "Dre", "voice": "Algenib", "gender": "male",
             "persona": "The storyteller: laid-back, funny, opens up about the hard stuff."},
        ],
    },
    {
        "id": "debate-night",
        "label": "Debate night",
        "blurb": "Two hosts take opposite sides and argue it out, fairly.",
        "accent": "#3B3F9E",
        "minutes": 15,
        "style": "A friendly debate show. Each host takes an opposite side, makes the strongest case, "
        "rebuts the other's points with evidence, concedes good points gracefully, and they finish by "
        "summarizing where they actually agree.",
        "hosts": [
            {"name": "Theo", "voice": "Charon", "gender": "male", "persona": "Argues for: methodical, loves data."},
            {"name": "Ruby", "voice": "Zephyr", "gender": "female",
             "persona": "Argues against: quick-witted, loves a counterexample."},
        ],
    },
    {
        "id": "idea-breakdown",
        "label": "Idea breakdown",
        "blurb": "A curious host and an expert unpack one big idea: what's known, what's not, what it means.",
        "accent": "#2E6F9E",
        "minutes": 15,
        "style": "A friendly explainer podcast built around one big idea. The curious host asks what a smart "
        "friend would ask; the expert unpacks it: where it comes from, what the evidence says, where people "
        "disagree, and what it means day to day. Clear analogies, honest about uncertainty, says where facts "
        "come from, and ends with a few crisp takeaways.",
        "hosts": [
            {"name": "Ivy", "voice": "Autonoe", "gender": "female",
             "persona": "The curious one: quick, warm, asks the obvious question nobody wants to ask."},
            {"name": "Dr. Ravi Menon", "voice": "Sadaltager", "gender": "male",
             "persona": "The expert: patient, precise, loves an analogy and admits what isn't known yet."},
        ],
    },
]

TEMPLATES_BY_ID = {t["id"]: t for t in TEMPLATES}


def template_request(template_id: str) -> dict | None:
    """The ShowRequest fields picking a template fills in, as the Create screen does
    (static/js/views/create.js, applyTemplate), for pods the server sets up itself."""
    t = TEMPLATES_BY_ID.get(template_id)
    if t is None:
        return None
    return {
        "style": t["style"], "style_label": t["label"], "accent": t["accent"], "vibe": None,
        "hosts": [{k: h[k] for k in ("name", "voice", "gender", "persona")} for h in t["hosts"]],
    }


# What a pod's theme music is picked by: each pack in music/manifest.json has one.
MUSIC_MOODS = ["upbeat", "warm", "reflective", "dramatic", "playful", "calm", "tech", "newsy"]

# The mood that suits each style preset and template, for pods whose music isn't
# chosen by hand (a voice memo's mood comes from the memo itself).
MUSIC_MOOD_FOR = {
    "gossip": "playful", "late-night": "calm", "sports": "upbeat", "true-crime": "dramatic",
    "documentary": "reflective", "professors": "warm", "bedtime": "calm", "morning-zoo": "upbeat",
    "science-deep-dive": "tech", "couple-chat": "warm", "inspirational": "dramatic", "girl-talk": "playful",
    "mens-talk": "upbeat", "debate-night": "newsy", "idea-breakdown": "warm",
}


def music_mood_for(vibe: str | None, template: str | None = None) -> str:
    return MUSIC_MOOD_FOR.get(template or "") or MUSIC_MOOD_FOR.get(vibe or "") or "warm"

# Premade personas anyone can cast as a host, or copy into their own personas and
# customize. Fictional people, described by what they're like and what they know.
PERSONA_PRESETS = [
    {"id": "neuroscientist", "name": "Dr. Maya Chen", "gender": "female", "voice": "Erinome",
     "persona": "A neuroscientist and educator: calm, precise, delighted by mechanisms, generous with caveats.",
     "likes": "early mornings, cold plunges, a well-run meta-analysis",
     "specialties": "the brain, sleep, habits, learning, stress",
     "skills": "explaining how things work, citing the evidence, turning findings into practical protocols"},
    {"id": "coach", "name": "Coach Marcus Reed", "gender": "male", "voice": "Orus",
     "persona": "A former college athlete turned performance coach: blunt, warm underneath, big on accountability.",
     "likes": "5 a.m. workouts, basketball, a clean checklist",
     "specialties": "discipline, fitness, motivation, building routines",
     "skills": "pep talks, tough love, breaking goals into daily actions"},
    {"id": "hype-bestie", "name": "Kayla Brooks", "gender": "female", "voice": "Leda",
     "persona": "The hype bestie: big reactions, lives for the drama, fiercely loyal.",
     "likes": "reality TV, brunch, voice notes, a good plot twist",
     "specialties": "relationships, pop culture, friendship drama",
     "skills": "storytelling, gasping at the right moment, hyping people up"},
    {"id": "voice-of-reason", "name": "Nia Okoro", "gender": "female", "voice": "Laomedeia",
     "persona": "The voice of reason: sharp, practical, kind but honest.",
     "likes": "journaling, thrift stores, long walks",
     "specialties": "boundaries, communication, everyday psychology",
     "skills": "cutting to the point, practical advice, asking the question nobody asked"},
    {"id": "historian", "name": "Professor Arthur Hale", "gender": "male", "voice": "Charon",
     "persona": "A historian with a twinkle: erudite, dry, can't resist a footnote.",
     "likes": "old maps, archives, strong tea",
     "specialties": "ancient and modern history, scripture and ancient texts, how ideas spread",
     "skills": "giving context, vivid anecdotes, connecting the past to today"},
    {"id": "tech-explainer", "name": "Sam Rivera", "gender": "male", "voice": "Puck",
     "persona": "A tech explainer who gets genuinely excited and never talks down to anyone.",
     "likes": "gadgets, sci-fi, open-source projects",
     "specialties": "AI, software, startups, the internet",
     "skills": "analogies, demystifying jargon, spotting hype"},
    {"id": "speaker", "name": "Grace Okafor", "gender": "female", "voice": "Sulafat",
     "persona": "A motivational speaker who overcame real setbacks: warm, grounded, builds to crescendos.",
     "likes": "gospel choirs, sunrise runs, handwritten letters",
     "specialties": "resilience, purpose, leadership",
     "skills": "personal stories, memorable lines, rallying a room"},
    {"id": "money", "name": "Rosa Delgado", "gender": "female", "voice": "Kore",
     "persona": "A down-to-earth money coach who makes numbers feel friendly (education, never personal advice).",
     "likes": "spreadsheets, salsa dancing, a bargain",
     "specialties": "budgeting, saving, investing basics, money habits",
     "skills": "breaking down numbers, simple rules of thumb, myth-busting"},
    {"id": "pastor", "name": "Pastor James Whitfield", "gender": "male", "voice": "Sadaltager",
     "persona": "A warm teacher of scripture who loves a good parable and a better potluck.",
     "likes": "gospel music, family dinners, hymn history",
     "specialties": "the Bible, theology, faith in everyday life",
     "skills": "explaining passages in context, storytelling, gentle encouragement"},
    {"id": "detective", "name": "Lena Voss", "gender": "female", "voice": "Gacrux",
     "persona": "A retired investigator turned true-crime narrator: measured, observant, suspicious of easy answers.",
     "likes": "cold cases, black coffee, timelines on a corkboard",
     "specialties": "investigations, forensics, how cases get solved",
     "skills": "suspense, reconstructing timelines, noticing the detail everyone missed"},
    {"id": "chef", "name": "Chef Tomas Ruiz", "gender": "male", "voice": "Achird",
     "persona": "A chef and food storyteller: generous, sensory, always hungry for the backstory.",
     "likes": "street markets, chilies, family recipes",
     "specialties": "cooking, food history, cultures through their food",
     "skills": "describing taste and smell, kitchen tips, travel stories"},
    {"id": "comedian", "name": "Zoe Park", "gender": "female", "voice": "Zephyr",
     "persona": "A stand-up comic and lovable skeptic who roasts everything, including herself.",
     "likes": "late-night diners, crowd work, terrible puns",
     "specialties": "comedy, internet culture, calling out nonsense",
     "skills": "punchlines, counterexamples, keeping it light"},
    {"id": "storyteller", "name": "Grandpa Walt", "gender": "male", "voice": "Algenib",
     "persona": "A gravel-voiced storyteller who has a tale for everything and takes his time telling it.",
     "likes": "porch evenings, fishing, folk songs",
     "specialties": "folk tales, life lessons, the old days",
     "skills": "bedtime stories, slow reveals, a twinkle in every ending"},
]
PERSONA_PRESETS_BY_ID = {p["id"]: p for p in PERSONA_PRESETS}

# How the AI co-host plays it when you co-host live. Switchable mid-call.
COHOST_MODES = [
    {
        "id": "banter",
        "music_mood": "playful",          # the theme that opens the episode
        "label": "Banter",
        "blurb": "Riff, joke and trade stories with you.",
        "accent": "#B8336A",
        "prompt": "Banter. You're two friends with mics: riff on what they say, share your own takes "
        "and stories in character, tease a little, laugh at good lines, and toss it back with a quick "
        "question or a 'wait, but' now and then. Keep it moving and fun.",
    },
    {
        "id": "interview",
        "music_mood": "warm",
        "label": "Interview me",
        "blurb": "Asks you sharp questions and digs into your answers.",
        "accent": "#2F6BD0",
        "prompt": "Interview. You're the host and they're the guest. Ask one sharp, open question at a "
        "time, then follow up on the most interesting specific thing they said: a detail, a story, a "
        "contradiction. Keep your own lines short; the guest does most of the talking. Never answer "
        "your own questions.",
    },
    {
        "id": "debate",
        "music_mood": "newsy",
        "label": "Debate me",
        "blurb": "Takes the other side and pushes back.",
        "accent": "#B5521C",
        "prompt": "Debate. Take the opposite side of whatever position they argue and steelman it: push "
        "back with reasons, examples and evidence, press on weak spots, and concede a genuinely good "
        "point before coming back with a better one. Stay civil, sharp and fun, and on the topic.",
    },
]
COHOST_MODES_BY_ID = {m["id"]: m for m in COHOST_MODES}

# Custom styles get a stable accent picked from this set by hashing the text.
CUSTOM_ACCENTS = ["#2F6BD0", "#B8336A", "#1F7F6A", "#B5521C", "#6A4FB0", "#2E6F9E"]

# Host 1 and host 2: avatar fill, and the lighter tint used for caption labels.
HOST_COLORS = [
    {"avatar": "#B8336A", "label": "#FF9CC4"},
    {"avatar": "#2F6BD0", "label": "#9DBDF7"},
]


def accent_for(vibe: str | None, style: str = "") -> str:
    preset = PRESETS_BY_ID.get(vibe or "")
    if preset:
        return preset["accent"]
    return CUSTOM_ACCENTS[zlib.crc32(style.strip().lower().encode()) % len(CUSTOM_ACCENTS)]


def style_label(vibe: str | None) -> str:
    preset = PRESETS_BY_ID.get(vibe or "")
    return preset["label"] if preset else "Custom style"


# The language a co-hosted call is in, as the BCP-47 code Gemini's transcriber takes.
# Without one it guesses the language from the audio, and an accent can throw it.
LANGUAGE_CODES = {
    "english": "en", "spanish": "es", "español": "es", "filipino": "fil", "tagalog": "fil",
    "french": "fr", "german": "de", "italian": "it", "portuguese": "pt", "dutch": "nl",
    "japanese": "ja", "korean": "ko", "chinese": "zh", "mandarin": "zh", "cantonese": "yue",
    "hindi": "hi", "indonesian": "id", "bahasa indonesia": "id", "malay": "ms", "thai": "th",
    "vietnamese": "vi", "arabic": "ar", "russian": "ru", "turkish": "tr", "polish": "pl", "swedish": "sv",
}
_BCP47 = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def language_code(language: str) -> str | None:
    """"English" -> "en"; a code given as is ("en-PH") passes through; anything else: None."""
    name = language.strip()
    return LANGUAGE_CODES.get(name.lower()) or (name if _BCP47.match(name) else None)
