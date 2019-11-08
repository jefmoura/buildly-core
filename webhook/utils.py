from rest_hooks.models import HOOK_EVENTS

from rest_hooks.models import Hook
from rest_hooks.client import Client


client = Client()


def find_and_fire_hook(event_name, instance, **kwargs):
    """
    Look up Hooks that apply
    """
    if event_name not in HOOK_EVENTS.keys():
        raise Exception(
            '"{}" does not exist in `settings.HOOK_EVENTS`.'.format(event_name)
        )

    filters = {'event': event_name}
    hooks = Hook.objects.filter(**filters)
    for hook in hooks:
        hook.target = 'http://productsservice:8080/webhook/'
        hook.deliver_hook(instance)
