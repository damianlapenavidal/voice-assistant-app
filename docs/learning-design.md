# Learning design

This document explains *how* the assistant teaches, and how the prompt content is
structured to make this a genuine language-learning experience for young children
(ages 3–6) rather than a talking toy that happens to say Spanish words.

The target learner is an **English-speaking child just beginning Spanish**. The
design goal is a companion that a child would want to talk to every day — a best
friend who happens to grow their second language almost without them noticing.

## Pedagogy: how children actually acquire a language

The assistant instructions are built on well-established second-language
acquisition principles for young children. These live in
[`teaching_method.md`](../src/voice_assistant/prompts/teaching_method.md), which
is composed into **every** persona so the whole app teaches the same way.

| Principle | What the assistant does |
| --- | --- |
| **Comprehensible input** | Wraps each Spanish word in enough English, tone, and context that meaning is clear before translating. The child never feels lost. |
| **Spaced repetition** | Introduces *one* new word at a time, then deliberately recycles it — later in the chat and in future chats. Review is treated as the core of learning, not filler. |
| **Chunks before rules** | Teaches whole usable phrases (`me gusta`, `¿qué es?`) and never explains grammar as rules. |
| **Comprehensible +1 (leveling)** | Reads the child's level (just starting / warming up / confident) and stretches just past it, dropping back the moment they seem unsure. |
| **Total Physical Response** | Ties words to movement, gesture, and sound the child can do. |
| **Low affective filter** | Speaking is always invited, never demanded. Silence is welcome. |
| **Recasting** | Mistakes are echoed back correctly and warmly, never corrected. |
| **Visible progress** | Names the child's growth ("You remembered gato!") to build confidence and habit. |

### The core vocabulary spine

`teaching_method.md` defines a shared, high-frequency **core spine** (greetings,
courtesy, numbers 1–10, colors, feelings, everyday words, cheers). Every persona
recycles this spine and stacks its own themed vocabulary on top, so a child hears
the same foundational words no matter which friend they play with — the natural,
low-tech form of spaced repetition across sessions.

## Prompt architecture

The system prompt is composed in three layers (see
[`loader.py`](../src/voice_assistant/prompts/loader.py)):

```
base_system.md      → who the assistant is + how it plays (identity, warmth,
                      "talk and listen", anti-repetition, safety)
teaching_method.md  → shared language pedagogy + core vocabulary spine
personas/<id>.md    → the character: themed vocab, songs, stories, games, voice
```

Keeping the method in its own layer means improving the pedagogy improves all
personas at once, and personas stay focused on content and character.

## Personas

Each persona is a distinct friend with its own themed content, real Spanish
children's songs, recurring story characters, and games. They are chosen to cover
complementary pedagogical roles:

| Persona | Character | Learning focus | Pedagogical role |
| --- | --- | --- | --- |
| `oso_animals` | Oso, a forest bear | animals, nature, weather | vocabulary + storytelling |
| `robi_colors` | Robi, a robot | colors, shapes, counting | numbers + categories |
| `chef_coco_food` | Chef Coco, a cook | food, cooking, manners | everyday words + courtesy |
| `luna_bedtime` | Luna, the moon | feelings, family, night | calm listening + emotional vocab |
| `pili_movement` | Pili, a wiggly friend | body, actions, movement | Total Physical Response |

Select a persona with the `PERSONA` environment variable (see `.env.example`).

## Content principles for editing personas

When adding or editing persona files, keep them consistent:

- **Themed vocab on top of the spine** — list words the persona owns; assume the
  core spine is always available from the method layer.
- **Real songs where possible** — traditional Spanish children's songs the model
  already knows are more valuable than invented ones.
- **Recurring story characters** — give the child friends to come back to.
- **Varied games, sounds as spice** — never lean on one mechanic (especially not
  sound effects) every turn.
- **Openers that don't force a reply** — a child should be able to just listen.

## Future direction

The biggest remaining lever is **per-child persistence**: remembering which words
an individual child has met and mastered, then driving true spaced-repetition
review and level progression across sessions. Today that adaptation happens
within the prompt (the assistant recycles and levels in the moment); making it
stateful — a small per-child vocabulary/level store the session reads and writes
— would turn "recycle what you taught" from an in-conversation instruction into a
durable, personalized curriculum.
