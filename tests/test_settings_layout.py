"""Tests for the settings dialog's geometry.

Nothing here touches Windows. The layout is a pure function of the model, the
text measurement it is handed, and how much of itself the tab control wraps
around a page — which is exactly why it lives apart from the adapter that
calls user32.
"""

from __future__ import annotations

from pathlib import Path

from claudemonitor.models import CLAUDE, CODEX, Insets
from claudemonitor.settings import (
    ProviderFields,
    SettingChoice,
    SettingCommand,
    SettingLink,
    SettingNumber,
    SettingOutput,
    SettingText,
    SettingToggle,
    SettingsGroup,
    SettingsModel,
    SettingsSection,
    SettingsTab,
    build_settings,
)
from claudemonitor.settings_layout import (
    _BUTTON_GAP,
    _BUTTON_HEIGHT,
    _BUTTON_MIN_WIDTH,
    _GROUP_PADDING,
    _OUTPUT_HEIGHT,
    _OUTPUT_MIN_WIDTH,
    _CHECKBOX_INDICATOR,
    _CHOICE_WIDTH,
    _TEXT_WIDTH,
    _GROUP_BOTTOM_PADDING,
    _GROUP_CAPTION_HEIGHT,
    _GROUP_GAP,
    _LIST_MIN_WIDTH,
    _ROW_GAP,
    _ROW_HEIGHT,
    _button_width,
    _content_width,
    _group_content_width,
    _group_height,
    _number_column,
    _place_buttons,
    _row_height,
    _tab_height,
    _tab_strip_width,
    settings_layout,
)


def _measure(text: str) -> int:
    """Stand in for GDI text measurement: a fixed width per character."""
    return 7 * len(text)


def _unscaled(value: int) -> int:
    """Stand in for DPI scaling at 100%, where a constant is its own pixels."""
    return value


def _toggle(key: str = "taskbar", label: str = "Show usage in the taskbar"):
    """One checkbox field, wired to nothing."""
    return SettingToggle(key=key, label=label, is_on=lambda: True, write=lambda _: None)


def _number(
    key: str = "poll_interval",
    label: str = "Check usage every",
    suffix: str = "seconds",
):
    """One number field, wired to nothing."""
    return SettingNumber(
        key=key,
        label=label,
        suffix=suffix,
        value=lambda: 60,
        write=lambda _: None,
        minimum=10,
        maximum=600,
    )


def _link(key: str = "log_folder", label: str = "Open log folder"):
    """One push button field, wired to nothing."""
    return SettingLink(key=key, label=label, open=lambda: None)


def _one_tab(*fields, title: str = "General", group: str = "Startup") -> SettingsModel:
    """Wrap the given fields in a single tab holding a single group."""
    return SettingsModel(
        tabs=[
            SettingsTab(
                title=title, groups=[SettingsGroup(title=group, fields=list(fields))]
            )
        ]
    )


def _text(key: str = "claude_model", label: str = "Model"):
    """One text box field, wired to nothing."""
    return SettingText(key=key, label=label, value=lambda: "haiku", write=lambda _: None)


def _choice(key: str = "claude_effort", label: str = "Reasoning effort"):
    """One drop-down field, wired to nothing."""
    return SettingChoice(
        key=key,
        label=label,
        value=lambda: "",
        write=lambda _: None,
        choices=(("", "Default"), ("low", "low")),
    )


def _provider_fields(provider) -> ProviderFields:
    """One provider's tab, wired to nothing."""
    return ProviderFields(
        provider=provider,
        tracking=SettingToggle(is_on=lambda: True, write=lambda _: None),
        renew_token=SettingToggle(is_on=lambda: True, write=lambda _: None),
        wake_session=SettingToggle(is_on=lambda: True, write=lambda _: None),
        cooldown=SettingNumber(
            value=lambda: 900, write=lambda _: None, minimum=60, maximum=86400
        ),
        model=SettingText(value=lambda: "haiku", write=lambda _: None),
        effort=SettingChoice(value=lambda: "", write=lambda _: None),
        copy_command=_command(),
        run_command=_command(),
        last_run=_output(),
    )


def _command(key: str = "claude_copy_command", label: str = "Copy command"):
    """One command button, wired to nothing."""
    return SettingCommand(key=key, label=label, act=lambda model, effort, changed: None)


def _output(key: str = "claude_last_run"):
    """One read-only output box, wired to nothing."""
    return SettingOutput(key=key, text=lambda: "Not run yet.")


def _production_model() -> SettingsModel:
    """Build the real model, so the tests bind to what ships."""
    return build_settings(
        taskbar=SettingToggle(is_on=lambda: True, write=lambda _: None),
        providers=[_provider_fields(CLAUDE), _provider_fields(CODEX)],
        startup=SettingToggle(is_on=lambda: True, write=lambda _: None),
        poll_interval=SettingNumber(
            value=lambda: 60, write=lambda _: None, minimum=10, maximum=600
        ),
        amber_threshold=SettingNumber(
            value=lambda: 50, write=lambda _: None, minimum=1, maximum=99
        ),
        red_threshold=SettingNumber(
            value=lambda: 20, write=lambda _: None, minimum=1, maximum=99
        ),
        log_dir=Path("."),
        open_url=lambda url: None,
        open_folder=lambda path: None,
    )


def _placed_groups(layout):
    """Every placed group with the width and height of the window it sits in.

    A tab's own groups sit on its page; a section's groups sit on the
    section's window, which is placed from its own top left corner.
    """
    placed = []
    for tab in layout.tabs:
        placed += [(group, layout.page.width, layout.page.height) for group in tab.groups]
        for section in tab.sections:
            placed += [
                (group, section.rect.width, section.rect.height) for group in section.groups
            ]
    return placed


def _sectioned(*sections, add_section=None) -> SettingsModel:
    """One tab whose groups are split into sections picked from a side list."""
    return SettingsModel(
        tabs=[SettingsTab(title="Providers", sections=list(sections), add_section=add_section)]
    )


def _section(title: str, *fields) -> SettingsSection:
    """One side-list entry holding a single group of the given fields."""
    return SettingsSection(
        title=title, groups=[SettingsGroup(title="Tracking", fields=list(fields))]
    )


# --------------------------------------------------------------- the whole layout


class TestSettingsLayout:
    """Where every control sits, worked out before a window exists."""

    def _layout(self, model: SettingsModel | None = None, **kwargs):
        return settings_layout(
            model if model is not None else _production_model(),
            measure=_measure,
            tab_frame=kwargs.pop("tab_frame", Insets(4, 24, 4, 4)),
            **kwargs,
        )

    def test_the_page_sits_inside_the_tab_control_by_its_frame(self):
        layout = self._layout(tab_frame=Insets(4, 24, 4, 4))

        assert layout.page.left == layout.tab.left + 4
        assert layout.page.top == layout.tab.top + 24
        assert layout.page.right == layout.tab.right - 4
        assert layout.page.bottom == layout.tab.bottom - 4

    def test_every_tab_is_laid_out(self):
        layout = self._layout()

        assert [tab.title for tab in layout.tabs] == ["General", "Providers", "Taskbar"]

    def test_every_group_and_field_is_placed(self):
        layout = self._layout()
        placed = {
            field.key for group, _width, _height in _placed_groups(layout) for field in group.fields
        }

        assert placed == {field.key for field in _production_model().fields()}

    def test_every_group_box_stays_inside_its_page(self):
        # Each page is a window of its own, so its contents are placed from
        # its own top left corner rather than from the frame's.
        layout = self._layout()

        for group, width, height in _placed_groups(layout):
            assert group.rect.left >= 0
            assert group.rect.top >= 0
            assert group.rect.right <= width
            assert group.rect.bottom <= height

    def test_groups_on_a_page_do_not_overlap(self):
        general = self._layout().tabs[0]

        bottoms = [group.rect.bottom for group in general.groups]
        tops = [group.rect.top for group in general.groups]
        assert all(top >= bottom for bottom, top in zip(bottoms, tops[1:]))

    def test_every_group_caption_sits_on_its_own_border(self):
        layout = self._layout()

        for group, _width, _height in _placed_groups(layout):
            assert group.caption.left > group.rect.left
            assert group.caption.top >= group.rect.top
            assert group.caption.right <= group.rect.right
            assert group.caption.bottom < group.rect.bottom

    def test_every_field_stays_inside_its_group(self):
        layout = self._layout()

        for group, _width, _height in _placed_groups(layout):
            for field in group.fields:
                assert field.rect.left >= group.rect.left
                assert field.rect.right <= group.rect.right
                assert field.rect.top >= group.rect.top
                assert field.rect.bottom <= group.rect.bottom

    def test_a_number_field_gets_a_box_a_spinner_and_a_unit(self):
        placed = self._layout(_one_tab(_number())).tabs[0].groups[0].fields[0]

        assert placed.editor is not None
        assert placed.spinner is not None
        assert placed.suffix is not None

    def test_the_spinner_sits_against_the_right_of_its_box(self):
        placed = self._layout(_one_tab(_number())).tabs[0].groups[0].fields[0]

        assert placed.spinner.right == placed.editor.right
        assert placed.spinner.top == placed.editor.top

    def test_the_unit_follows_the_box(self):
        placed = self._layout(_one_tab(_number())).tabs[0].groups[0].fields[0]

        assert placed.suffix.left >= placed.editor.right

    def test_two_numbers_in_one_group_share_a_column(self):
        # Two numbers under one caption read as a pair, so their boxes line up
        # with each other rather than each following its own label.
        short = _number(key="red_threshold", label="Red")
        long = _number(key="amber_threshold", label="A much longer label")
        placed = self._layout(_one_tab(short, long)).tabs[0].groups[0].fields

        assert placed[0].editor.left == placed[1].editor.left

    def test_a_long_unit_beside_a_short_label_is_not_clipped(self):
        short = _number(key="red_threshold", label="Red")
        long = _number(key="amber_threshold", label="A much longer label")
        layout = self._layout(_one_tab(short, long))
        placed = layout.tabs[0].groups[0].fields

        assert all(field.suffix.right <= layout.page.width for field in placed)

    def test_a_toggle_gets_no_box_of_its_own(self):
        placed = self._layout(_one_tab(_toggle())).tabs[0].groups[0].fields[0]

        assert placed.editor is None and placed.spinner is None

    def test_every_page_is_the_same_size(self):
        # The tab control shows one page at a time in one rectangle, so the
        # window is sized for the busiest page and the others keep that size.
        layout = self._layout()

        assert layout.page.height > 0
        assert all(group.rect.bottom <= height for group, _width, height in _placed_groups(layout))

    def test_the_window_is_wide_enough_for_the_longest_label(self):
        layout = self._layout(_one_tab(_toggle(label="A" * 90)))

        assert layout.width >= _measure("A" * 90)

    def test_the_three_buttons_sit_in_a_row_below_the_tab_control(self):
        layout = self._layout()

        assert layout.ok.top > layout.tab.bottom
        assert layout.ok.top == layout.cancel.top == layout.apply.top
        assert layout.ok.right <= layout.cancel.left
        assert layout.cancel.right <= layout.apply.left

    def test_the_button_row_ends_at_the_right_margin(self):
        layout = self._layout()

        assert layout.apply.right < layout.width
        assert layout.width - layout.apply.right == layout.tab.left

    def test_the_window_is_tall_enough_for_the_button_row(self):
        layout = self._layout()

        assert layout.height > layout.apply.bottom

    def test_a_higher_dpi_scales_every_measurement(self):
        normal = self._layout(dpi=96)
        scaled = self._layout(dpi=192)

        assert scaled.height > normal.height
        assert scaled.tab.left == 2 * normal.tab.left

    def test_a_model_with_no_tabs_still_produces_a_usable_window(self):
        layout = self._layout(SettingsModel(tabs=[]))

        assert layout.width > 0 and layout.height > 0


class TestASectionedTab:
    """A list down the left of the page, and the picked section beside it."""

    def _layout(self, model: SettingsModel):
        return settings_layout(model, measure=_measure, tab_frame=Insets(4, 24, 4, 4))

    def test_a_plain_tab_has_no_list(self):
        tab = self._layout(_one_tab(_toggle())).tabs[0]

        assert tab.side_list is None and tab.add_button is None and tab.sections == []

    def test_a_sectioned_tab_gets_a_list_on_the_left_of_its_page(self):
        tab = self._layout(_sectioned(_section("Claude", _toggle()))).tabs[0]

        assert tab.side_list is not None
        assert tab.side_list.left > 0 and tab.side_list.top > 0

    def test_every_section_is_laid_out_in_order(self):
        tab = self._layout(
            _sectioned(_section("Claude", _toggle()), _section("Codex", _toggle(key="b")))
        ).tabs[0]

        assert [section.title for section in tab.sections] == ["Claude", "Codex"]

    def test_every_section_sits_right_of_the_list(self):
        tab = self._layout(
            _sectioned(_section("Claude", _toggle()), _section("Codex", _toggle(key="b")))
        ).tabs[0]

        assert all(section.rect.left >= tab.side_list.right for section in tab.sections)

    def test_every_section_shares_one_rectangle(self):
        # Only one section shows at a time, in the same place.
        tab = self._layout(
            _sectioned(_section("Claude", _toggle()), _section("Codex", _toggle(key="b")))
        ).tabs[0]

        assert tab.sections[0].rect == tab.sections[1].rect

    def test_a_section_stays_inside_the_page(self):
        layout = self._layout(_sectioned(_section("Claude", _toggle(label="A" * 60))))
        section = layout.tabs[0].sections[0]

        assert section.rect.right <= layout.page.width
        assert section.rect.bottom <= layout.page.height

    def test_the_first_group_lines_up_with_the_top_of_the_list(self):
        layout = self._layout(_sectioned(_section("Claude", _toggle())))
        tab = layout.tabs[0]

        assert tab.sections[0].rect.top + tab.sections[0].groups[0].rect.top == tab.side_list.top

    def test_the_list_is_at_least_its_minimum_width(self):
        tab = self._layout(_sectioned(_section("C", _toggle()))).tabs[0]

        assert tab.side_list.width >= _LIST_MIN_WIDTH

    def test_a_long_section_title_widens_the_list(self):
        tab = self._layout(_sectioned(_section("A" * 30, _toggle()))).tabs[0]

        assert tab.side_list.width > _measure("A" * 30)

    def test_the_page_is_wide_enough_for_the_list_and_the_widest_group(self):
        layout = self._layout(_sectioned(_section("Claude", _toggle(label="A" * 60))))

        assert layout.page.width > _LIST_MIN_WIDTH + _measure("A" * 60)

    def test_the_page_is_tall_enough_for_the_tallest_section(self):
        short = _section("Claude", _toggle())
        tall = _section("Codex", *[_toggle(key=f"t{n}") for n in range(8)])
        layout = self._layout(_sectioned(short, tall))

        assert layout.page.height >= _group_height(tall.groups[0], _unscaled)

    def test_without_an_add_action_the_list_runs_to_the_bottom(self):
        layout = self._layout(_sectioned(_section("Claude", _toggle())))
        tab = layout.tabs[0]

        assert tab.add_button is None
        assert tab.side_list.bottom == layout.page.height - tab.side_list.top

    def test_an_add_action_gets_a_button_under_the_list(self):
        layout = self._layout(
            _sectioned(_section("Claude", _toggle()), add_section=_link("add", "Add provider…"))
        )
        tab = layout.tabs[0]

        assert tab.add_button.top > tab.side_list.bottom
        assert tab.add_button.left == tab.side_list.left
        assert tab.add_button.bottom <= layout.page.height

    def test_the_add_button_is_as_wide_as_the_list(self):
        tab = self._layout(
            _sectioned(_section("Claude", _toggle()), add_section=_link("add", "Add provider…"))
        ).tabs[0]

        assert tab.add_button.width == tab.side_list.width

    def test_a_long_add_label_widens_the_list(self):
        label = "Add a provider that has a long name"
        tab = self._layout(
            _sectioned(_section("C", _toggle()), add_section=_link("add", label))
        ).tabs[0]

        assert tab.side_list.width >= _button_width(label, _measure, _unscaled)

    def test_the_production_providers_tab_lists_every_provider(self):
        layout = self._layout(_production_model())
        providers = layout.tabs[1]

        assert [section.title for section in providers.sections] == ["Claude", "Codex"]
        assert providers.groups == []


# -------------------------------------------------------------- the parts of it


class TestTextAndChoiceFields:
    """A model box and an effort list sit on the same column a number does."""

    def _placed(self, *fields):
        layout = settings_layout(
            _one_tab(*fields), measure=_measure, tab_frame=Insets(4, 25, 4, 4)
        )
        return {placed.key: placed for placed in layout.tabs[0].groups[0].fields}

    def test_a_text_field_gets_a_label_and_a_box(self):
        placed = self._placed(_text())["claude_model"]

        assert placed.editor is not None
        assert placed.editor.width == _TEXT_WIDTH
        assert placed.spinner is None

    def test_a_choice_gets_a_label_and_a_list(self):
        placed = self._placed(_choice())["claude_effort"]

        assert placed.editor is not None
        assert placed.editor.width == _CHOICE_WIDTH

    def test_the_label_sits_before_the_box(self):
        placed = self._placed(_text())["claude_model"]

        assert placed.rect.right <= placed.editor.left

    def test_a_number_a_text_and_a_choice_share_one_column(self):
        placed = self._placed(
            _number(key="claude_cooldown", label="Wait between runs"),
            _text(),
            _choice(),
        )

        lefts = {placed[key].editor.left for key in placed}
        assert len(lefts) == 1

    def test_a_long_choice_label_moves_the_shared_column(self):
        short = self._placed(_number(label="Wait"), _text(label="Model"))
        longer = self._placed(
            _number(label="Wait"), _text(label="Model"), _choice(label="A much longer label")
        )

        assert longer["claude_model"].editor.left > short["claude_model"].editor.left


class TestRowHeight:
    """Only a push button is taller than an ordinary row."""

    def test_a_button_gets_the_taller_row(self):
        assert _row_height(_link(), _unscaled) == _BUTTON_HEIGHT

    def test_a_toggle_gets_the_ordinary_row(self):
        assert _row_height(_toggle(), _unscaled) == _ROW_HEIGHT

    def test_a_number_gets_the_ordinary_row(self):
        assert _row_height(_number(), _unscaled) == _ROW_HEIGHT


class TestButtonWidth:
    """A button is as wide as its own text, but never narrower than the minimum."""

    def test_a_short_label_still_gets_the_minimum_width(self):
        assert _button_width("OK", _measure, _unscaled) == _BUTTON_MIN_WIDTH

    def test_a_long_label_widens_the_button_past_the_minimum(self):
        wide = _button_width("A" * 40, _measure, _unscaled)

        assert wide > _BUTTON_MIN_WIDTH
        assert wide > _measure("A" * 40)


class TestContentWidth:
    """What one field asks for inside its group box."""

    def test_a_toggle_asks_for_its_box_plus_its_label(self):
        width = _content_width(_toggle(label="Track"), _measure, _unscaled, column=0)

        assert width == _CHECKBOX_INDICATOR + _measure("Track")

    def test_a_number_is_measured_from_the_shared_column(self):
        narrow = _content_width(_number(), _measure, _unscaled, column=0)
        wide = _content_width(_number(), _measure, _unscaled, column=50)

        assert wide - narrow == 50

    def test_a_link_asks_for_its_button(self):
        width = _content_width(_link(), _measure, _unscaled, column=0)

        assert width == _button_width("Open log folder", _measure, _unscaled)


class TestNumberColumn:
    """Every number box in one group starts at the same x."""

    def test_the_column_clears_the_longest_label_in_the_group(self):
        short = _number(key="a", label="Red")
        long = _number(key="b", label="A much longer label")

        column = _number_column([short, long], 10, _measure, _unscaled)

        assert column > 10 + _measure("A much longer label")

    def test_a_group_with_no_numbers_starts_at_its_own_left_edge(self):
        assert _number_column([_toggle()], 10, _measure, _unscaled) > 10

    def test_a_toggles_label_does_not_move_the_column(self):
        with_toggle = _number_column(
            [_number(label="Red"), _toggle(label="A" * 90)], 0, _measure, _unscaled
        )
        alone = _number_column([_number(label="Red")], 0, _measure, _unscaled)

        assert with_toggle == alone


class TestGroupSize:
    """A group box is its caption, its rows, and the padding under them."""

    def test_one_group_is_its_caption_plus_its_rows(self):
        group = SettingsGroup(title="Polling", fields=[_number()])

        assert _group_height(group, _unscaled) == (
            _GROUP_CAPTION_HEIGHT + _ROW_HEIGHT + _GROUP_BOTTOM_PADDING
        )

    def test_a_second_row_adds_its_height_and_one_gap(self):
        one = _group_height(SettingsGroup(title="P", fields=[_number()]), _unscaled)
        two = _group_height(
            SettingsGroup(title="P", fields=[_number(), _number(key="b")]), _unscaled
        )

        assert two - one == _ROW_HEIGHT + _ROW_GAP

    def test_an_empty_group_is_only_its_caption_and_padding(self):
        empty = SettingsGroup(title="P", fields=[])

        assert _group_height(empty, _unscaled) == (
            _GROUP_CAPTION_HEIGHT + _GROUP_BOTTOM_PADDING
        )

    def test_a_groups_width_is_that_of_its_widest_field(self):
        group = SettingsGroup(
            title="P", fields=[_toggle(label="Short"), _toggle(key="b", label="A" * 40)]
        )

        assert _group_content_width(group, _measure, _unscaled) == (
            _CHECKBOX_INDICATOR + _measure("A" * 40)
        )

    def test_an_empty_group_asks_for_no_width(self):
        assert _group_content_width(SettingsGroup(title="P"), _measure, _unscaled) == 0


class TestTabSize:
    """A page is its groups stacked; the strip is its captions side by side."""

    def test_a_page_stacks_its_groups_with_a_gap_between_them(self):
        group = SettingsGroup(title="P", fields=[_number()])
        one = _tab_height(SettingsTab(title="T", groups=[group]), _unscaled)
        two = _tab_height(SettingsTab(title="T", groups=[group, group]), _unscaled)

        assert two - one == _group_height(group, _unscaled) + _GROUP_GAP

    def test_an_empty_page_needs_no_height(self):
        assert _tab_height(SettingsTab(title="T", groups=[]), _unscaled) == 0

    def test_the_strip_is_as_wide_as_every_caption_side_by_side(self):
        model = SettingsModel(
            tabs=[SettingsTab(title="General"), SettingsTab(title="Taskbar")]
        )

        width = _tab_strip_width(model, _measure, _unscaled)

        assert width > _measure("General") + _measure("Taskbar")

    def test_a_model_with_no_tabs_needs_no_strip(self):
        assert _tab_strip_width(SettingsModel(tabs=[]), _measure, _unscaled) == 0


class TestButtonRow:
    """OK, Cancel, and Apply end at the right margin, in that order."""

    def test_the_row_ends_at_the_right_edge_it_is_given(self):
        ok, cancel, apply_button = _place_buttons(
            [80, 80, 80], right=500, top=10, scaled=_unscaled
        )

        assert apply_button.right == 500
        assert ok.left < cancel.left < apply_button.left

    def test_every_button_shares_one_row(self):
        ok, cancel, apply_button = _place_buttons(
            [80, 90, 100], right=500, top=10, scaled=_unscaled
        )

        assert ok.top == cancel.top == apply_button.top == 10
        assert ok.bottom == apply_button.bottom == 10 + _BUTTON_HEIGHT

    def test_each_button_keeps_the_width_it_was_given(self):
        ok, cancel, apply_button = _place_buttons(
            [80, 90, 100], right=500, top=10, scaled=_unscaled
        )

        assert (ok.width, cancel.width, apply_button.width) == (80, 90, 100)


class TestCommandsAndTheirOutput:
    """Copy command and Run now share a row; the box under them spans the group."""

    def _group(self, *fields):
        layout = settings_layout(
            _one_tab(*fields, group="Auto-refresh"),
            measure=_measure,
            tab_frame=Insets(4, 24, 4, 4),
            dpi=96,
        )
        return layout.tabs[0].groups[0]

    def _two_buttons_and_a_box(self):
        return self._group(
            _command(),
            _command(key="claude_run_command", label="Run now"),
            _output(),
        )

    def test_two_buttons_in_a_row_share_one_line(self):
        copy, run, _box = self._two_buttons_and_a_box().fields

        assert (copy.rect.top, copy.rect.bottom) == (run.rect.top, run.rect.bottom)

    def test_the_second_button_follows_the_first_after_a_gap(self):
        copy, run, _box = self._two_buttons_and_a_box().fields

        assert run.rect.left == copy.rect.right + _BUTTON_GAP

    def test_each_button_keeps_the_width_of_its_own_label(self):
        copy, run, _box = self._two_buttons_and_a_box().fields

        assert copy.rect.width == _button_width("Copy command", _measure, _unscaled)
        assert run.rect.width == _button_width("Run now", _measure, _unscaled)

    def test_a_button_after_a_different_field_starts_a_row_of_its_own(self):
        toggle, link = self._group(_toggle(), _link()).fields

        assert link.rect.top > toggle.rect.bottom

    def test_the_box_sits_below_the_buttons(self):
        copy, _run, box = self._two_buttons_and_a_box().fields

        assert box.rect.top == copy.rect.bottom + _ROW_GAP

    def test_the_box_spans_the_group_inside_its_padding(self):
        group = self._two_buttons_and_a_box()
        box = group.fields[2]

        assert box.rect.left == group.rect.left + _GROUP_PADDING
        assert box.rect.right == group.rect.right - _GROUP_PADDING

    def test_the_box_is_several_lines_tall(self):
        box = self._two_buttons_and_a_box().fields[2]

        assert box.rect.height == _OUTPUT_HEIGHT

    def test_a_button_row_is_one_row_high(self):
        group = SettingsGroup(
            title="P", fields=[_command(), _command(key="b", label="Run now")]
        )

        assert _group_height(group, _unscaled) == (
            _GROUP_CAPTION_HEIGHT + _BUTTON_HEIGHT + _GROUP_BOTTOM_PADDING
        )

    def test_a_button_row_is_as_wide_as_its_buttons_and_the_gap(self):
        group = SettingsGroup(
            title="P", fields=[_command(), _command(key="b", label="Run now")]
        )

        assert _group_content_width(group, _measure, _unscaled) == (
            _button_width("Copy command", _measure, _unscaled)
            + _BUTTON_GAP
            + _button_width("Run now", _measure, _unscaled)
        )

    def test_the_box_asks_for_its_minimum_width(self):
        assert _content_width(_output(), _measure, _unscaled, column=0) == _OUTPUT_MIN_WIDTH

    def test_the_box_gets_a_row_of_its_own_height(self):
        assert _row_height(_output(), _unscaled) == _OUTPUT_HEIGHT

    def test_a_command_gets_the_button_row(self):
        assert _row_height(_command(), _unscaled) == _BUTTON_HEIGHT

    def test_the_production_refresh_group_holds_both_buttons_on_one_row(self):
        layout = settings_layout(
            _production_model(), measure=_measure, tab_frame=Insets(4, 24, 4, 4)
        )
        refresh, last_run = layout.tabs[1].sections[0].groups[1:3]
        placed = {field.key: field for field in refresh.fields}
        (box,) = last_run.fields

        assert placed["claude_copy_command"].rect.top == placed["claude_run_command"].rect.top
        assert box.key == "claude_last_run"
        assert last_run.rect.top > refresh.rect.bottom
        assert box.rect.bottom <= last_run.rect.bottom
