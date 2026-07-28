"""Bounded CapCut podcast-short template used to seed one Guide planning call."""

from __future__ import annotations

from .guide_models import GuidePlanningStep

CAPCUT_TEMPLATE_VERSION = "capcut-podcast-short-v1"

CAPCUT_ACCEPTANCE_CRITERIA = [
    "Timeline duration is between 58 and 60 seconds.",
    "Caption track is visible and the user reviewed names and punctuation.",
    "Music layer is visible and the user confirms dialogue remains clear.",
    "Canvas is 9:16 and the preview is portrait.",
    "Speaker is visibly inside the portrait safe area.",
    "User confirms the full clip was played and reviewed.",
    "CapCut shows export success at 1080x1920 and MP4 output evidence is available.",
]

CAPCUT_CONSTRAINTS = [
    "Guide a beginner with one visible action at a time.",
    "Do not choose a passage from audio semantics Aura cannot hear.",
    "Do not advance without current-screen evidence.",
    "Do not complete without full-playback review and export evidence.",
]

CAPCUT_STEPS = [
    GuidePlanningStep(
        step_id="confirm_project",
        title="Confirm project",
        expected_user_action="Open the podcast project and expose the source duration.",
        expected_duration_seconds=45,
        verification_predicates=["project_visible", "source_duration_visible"],
    ),
    GuidePlanningStep(
        step_id="choose_passage",
        title="Choose passage",
        dependencies=["confirm_project"],
        expected_user_action="Play and stop at a strong moment, then confirm start and end.",
        expected_duration_seconds=300,
        verification_predicates=["user_confirmed_start", "user_confirmed_end"],
        critical=True,
    ),
    GuidePlanningStep(
        step_id="trim_duration",
        title="Trim to 60 seconds",
        dependencies=["choose_passage"],
        expected_user_action="Trim the timeline to a 58 to 60 second passage.",
        expected_duration_seconds=180,
        verification_predicates=["timeline_duration_58_60"],
        critical=True,
    ),
    GuidePlanningStep(
        step_id="generate_captions",
        title="Generate captions",
        dependencies=["trim_duration"],
        expected_user_action="Generate automatic captions for the clip.",
        expected_duration_seconds=300,
        verification_predicates=["caption_track_visible"],
    ),
    GuidePlanningStep(
        step_id="review_captions",
        title="Review captions",
        dependencies=["generate_captions"],
        expected_user_action="Review caption names and punctuation.",
        expected_duration_seconds=180,
        verification_predicates=["caption_track_visible", "user_confirmed_caption_review"],
        critical=True,
    ),
    GuidePlanningStep(
        step_id="add_music",
        title="Add music",
        dependencies=["review_captions"],
        expected_user_action="Add a music track under the clip.",
        expected_duration_seconds=120,
        verification_predicates=["music_layer_visible"],
    ),
    GuidePlanningStep(
        step_id="lower_music",
        title="Lower music",
        dependencies=["add_music"],
        expected_user_action="Lower music volume beneath dialogue.",
        expected_duration_seconds=90,
        verification_predicates=["music_layer_visible", "user_confirms_dialogue_clear"],
        critical=True,
    ),
    GuidePlanningStep(
        step_id="set_vertical_canvas",
        title="Set vertical canvas",
        dependencies=["lower_music"],
        expected_user_action="Set the canvas ratio to 9:16.",
        expected_duration_seconds=60,
        verification_predicates=["canvas_9_16_visible", "portrait_preview_visible"],
        critical=True,
    ),
    GuidePlanningStep(
        step_id="reframe_speaker",
        title="Reframe speaker",
        dependencies=["set_vertical_canvas"],
        expected_user_action="Reframe the speaker inside the portrait safe area.",
        expected_duration_seconds=120,
        verification_predicates=["speaker_inside_portrait_safe_area"],
        critical=True,
    ),
    GuidePlanningStep(
        step_id="review_full_clip",
        title="Review full clip",
        dependencies=["reframe_speaker"],
        expected_user_action="Play the full clip and report any issue.",
        expected_duration_seconds=120,
        verification_predicates=["full_playback_finished", "user_confirmed_review"],
        critical=True,
    ),
    GuidePlanningStep(
        step_id="repair_issue",
        title="Repair reported issue",
        dependencies=["review_full_clip"],
        expected_user_action="Repair any issue the user reports, or confirm none remain.",
        expected_duration_seconds=180,
        verification_predicates=["user_confirms_no_open_issue"],
        critical=True,
    ),
    GuidePlanningStep(
        step_id="export_vertical_mp4",
        title="Export MP4",
        dependencies=["repair_issue"],
        expected_user_action="Export an MP4 at 1080x1920.",
        expected_duration_seconds=300,
        verification_predicates=["export_1080x1920_selected", "export_success_visible"],
        critical=True,
    ),
    GuidePlanningStep(
        step_id="verify_export",
        title="Verify exported file",
        dependencies=["export_vertical_mp4"],
        expected_user_action="Verify the exported MP4 file and its metadata.",
        expected_duration_seconds=90,
        verification_predicates=["export_success_visible", "output_mp4_evidence"],
        critical=True,
    ),
]


def compact_capcut_template() -> str:
    lines = [
        f"Template version: {CAPCUT_TEMPLATE_VERSION}",
        "Required order:",
    ]
    for index, step in enumerate(CAPCUT_STEPS, start=1):
        lines.append(
            f"{index}. {step.step_id}: {step.expected_user_action} "
            f"Verify {', '.join(step.verification_predicates)}."
        )
    lines.extend(
        [
            "Acceptance evidence:",
            *[f"- {criterion}" for criterion in CAPCUT_ACCEPTANCE_CRITERIA],
        ]
    )
    return "\n".join(lines)
