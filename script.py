# -*- coding: utf-8 -*-
"""QuickHide 2.0 — one pick, one compact decision popup."""

from pyrevit import revit, forms, script
from quickhide import core

logger = script.get_logger()
doc = revit.doc
uidoc = revit.uidoc


def _describe(target):
    lines = [
        target.category_name,
        target.family_name or '(family unavailable)',
        target.type_name or '(type unavailable)',
    ]
    if target.mark:
        lines.append('Mark: {}'.format(target.mark))
    lines.append('Source: {}'.format(target.source_name))
    return '\n'.join(lines)


def _pick_action(target, view, template):
    """One popup: command chooses match; switch chooses template scope."""
    exact_ok, exact_reason = core.can_filter_selected_instance(target, doc)

    commands = []
    if not target.is_linked:
        commands.append('Hide this element')
    elif exact_ok:
        commands.append('Hide this element (unique Mark)')
    commands.append('Hide all similar elements')

    switches = []
    if template is not None:
        switches.append('Apply to all views using template: {}'.format(template.Name))

    message = _describe(target)
    if target.is_linked and not exact_ok:
        message += ('\n\nExact linked-element hiding is unavailable: {}\n'
                    'Choose All Similar to match category + family/type.'
                    .format(exact_reason))

    result = forms.CommandSwitchWindow.show(
        commands,
        switches=switches,
        message=message,
        title='QuickHide 2.0')
    if not result:
        return None, False

    if isinstance(result, tuple):
        command, switch_values = result
    else:
        command, switch_values = result, {}

    apply_template = False
    if switches:
        apply_template = bool(switch_values.get(switches[0], False))
    return command, apply_template


def _confirm_filter_effect(target, destination, match_mode, view_count):
    match_text = ('unique Mark {}'.format(target.mark)
                  if match_mode == 'instance'
                  else 'same category + family/type')
    lines = [
        'QuickHide must use a filter for this operation.',
        '',
        'Match: {}'.format(match_text),
        'Destination: {}'.format(destination.Name),
    ]
    if view_count is not None:
        lines.append('Views controlled by template: {}'.format(view_count))
    lines += [
        '',
        'Matching elements may also be affected in other visible links or the host model.',
        'Continue?'
    ]
    return forms.alert('\n'.join(lines), title='QuickHide 2.0',
                       ok=False, yes=True, no=True)


def _choose_mode(view):
    """Return (scope, multiple) or (None, None) if cancelled.

    scope is 'host' or 'link'. 'multiple' is a bool. We only ask about
    host-vs-link when the view actually contains links; the single-vs-multiple
    choice is offered as a toggle so the common single-click stays one step.
    """
    has_links = core.view_has_visible_links(doc, view)
    multi_switch = 'Select multiple elements'

    if has_links:
        buttons = ['This model (host)', 'Inside a linked model']
    else:
        # No links: scope is implicitly host, but still offer multi as a button
        # so the user can opt in without an extra dialog.
        buttons = ['Select one element', 'Select multiple elements']

    result = forms.CommandSwitchWindow.show(
        buttons,
        switches=[multi_switch] if has_links else [],
        message='Choose what to pick.' if has_links
                else 'Pick one element, or switch to multiple.',
        title='QuickHide 2.0')
    if not result:
        return None, None

    if isinstance(result, tuple):
        command, switch_values = result
    else:
        command, switch_values = result, {}
    if not command:
        return None, None

    if has_links:
        scope = 'link' if command.startswith('Inside') else 'host'
        multiple = bool(switch_values.get(multi_switch, False))
    else:
        scope = 'host'
        multiple = command.startswith('Select multiple')
    return scope, multiple


def _select_targets(view):
    """Run the chooser and the correct picker. Returns list of targets or None."""
    scope, multiple = _choose_mode(view)
    if scope is None:
        return None

    if scope == 'link':
        if multiple:
            targets = core.pick_linked_targets(uidoc)
        else:
            single = core.pick_linked_target_single(uidoc)
            if single == 'not_model':
                forms.alert(
                    'That was a line, tag, or other non-model object. '
                    'QuickHide only targets model elements. Try again and TAB '
                    'to the family.', title='QuickHide 2.0', exitscript=True)
            targets = None if single in (None, 'not_model') else [single]
    else:
        if multiple:
            targets = core.pick_host_targets(uidoc)
        else:
            single = core.pick_host_target_single(uidoc)
            targets = None if single is None else [single]

    if not targets:
        return None
    return targets


def _hide_multiple(targets, view, template):
    """Decision + execution path for a multi-element selection.

    Semantics differ from single-pick: the choices are 'hide these exact
    elements in this view' (host only, direct hide) or 'hide all similar to
    these' (filters). Per-element unique-Mark isolation is single-pick only.
    """
    any_linked = any(t.is_linked for t in targets)

    commands = []
    # Direct in-view hide is possible only for host elements.
    if not any_linked:
        commands.append('Hide these {} elements (this view)'.format(len(targets)))
    commands.append('Hide all similar to these')

    switches = []
    if template is not None:
        switches.append('Apply to all views using template: {}'.format(template.Name))

    msg = '{} elements selected ({}).'.format(
        len(targets), 'linked' if any_linked else 'host')
    if any_linked:
        msg += ('\n\nLinked elements cannot be hidden individually, so '
                '"similar" filters (category + family/type) are used.')

    result = forms.CommandSwitchWindow.show(
        commands, switches=switches, message=msg, title='QuickHide 2.0')
    if not result:
        return
    if isinstance(result, tuple):
        command, switch_values = result
    else:
        command, switch_values = result, {}
    if not command:
        return

    apply_template = bool(switch_values.get(switches[0], False)) if switches else False
    if apply_template and template is None:
        forms.alert('The active view has no assigned View Template.',
                    title='QuickHide 2.0', exitscript=True)

    direct_hide = command.startswith('Hide these')

    # Direct, current-view hide of host elements -----------------------------
    if direct_hide and not apply_template:
        hideable, blocked = core.split_hideable(targets, view)
        if not hideable:
            forms.alert('None of the selected elements can be hidden here.',
                        title='QuickHide 2.0', exitscript=True)
        core.hide_in_view(hideable, view, doc)
        out = script.get_output()
        out.print_md('**QuickHide:** hid **{}** element(s) in *{}*.{}'.format(
            len(hideable), view.Name,
            '  Skipped {}.'.format(len(blocked)) if blocked else ''))
        return

    # Everything else routes through similar-filters -------------------------
    destination = template if apply_template else view
    affected = core.views_using_template(template, doc) if apply_template else None
    view_count = len(affected) if affected is not None else None

    lines = [
        'QuickHide will create/apply "similar" filters for {} element(s).'.format(
            len(targets)),
        'Match: same category + family/type (per element).',
        'Destination: {}'.format(destination.Name),
    ]
    if view_count is not None:
        lines.append('Views controlled by template: {}'.format(view_count))
    lines += ['', 'This may also affect matching elements elsewhere. Continue?']
    if not forms.alert('\n'.join(lines), title='QuickHide 2.0',
                       ok=False, yes=True, no=True):
        return

    try:
        core.apply_target_filters(targets, destination, doc, 'similar')
    except Exception as err:
        logger.exception('QuickHide 2.0 multi-filter failed')
        forms.alert('QuickHide could not apply the filters.\n\n{}'.format(err),
                    title='QuickHide 2.0', exitscript=True)


def main():
    view, reason = core.get_hideable_active_view(doc)
    if view is None:
        forms.alert(reason, title='QuickHide 2.0', exitscript=True)

    targets = _select_targets(view)
    if not targets:
        script.exit()

    template = core.get_assigned_template(view, doc)

    # Multiple elements use their own, simpler decision path.
    if len(targets) > 1:
        _hide_multiple(targets, view, template)
        return

    # Single element keeps the full per-element decision (incl. unique Mark).
    target = targets[0]
    command, apply_template = _pick_action(target, view, template)
    if not command:
        script.exit()

    if apply_template and template is None:
        forms.alert('The active view has no assigned View Template.',
                    title='QuickHide 2.0', exitscript=True)

    hide_similar = command.startswith('Hide all similar')
    match_mode = 'similar' if hide_similar else 'instance'

    # Fastest path: exact host element, current view. No second confirmation.
    if not target.is_linked and not apply_template and not hide_similar:
        hideable, blocked = core.split_hideable([target], view)
        if not hideable:
            reason = blocked[0][1] if blocked else 'Element cannot be hidden.'
            forms.alert(reason, title='QuickHide 2.0', exitscript=True)
        core.hide_in_view(hideable, view, doc)
        return

    destination = template if apply_template else view
    affected = core.views_using_template(template, doc) if apply_template else None
    view_count = len(affected) if affected is not None else None

    # Host + template + "this" also needs a unique filter rule.
    if not hide_similar:
        exact_ok, exact_reason = core.can_filter_selected_instance(target, doc)
        if not exact_ok:
            forms.alert(
                'This element cannot be isolated safely in a filter.\n\n{}\n\n'
                'Use Hide all similar elements instead.'.format(exact_reason),
                title='QuickHide 2.0', exitscript=True)

    if not _confirm_filter_effect(target, destination, match_mode, view_count):
        script.exit()

    try:
        core.apply_target_filters([target], destination, doc, match_mode)
    except Exception as err:
        logger.exception('QuickHide 2.0 failed')
        forms.alert(
            'QuickHide could not create or apply the filter.\n\n{}'.format(err),
            title='QuickHide 2.0', exitscript=True)


if __name__ == '__main__':
    main()
