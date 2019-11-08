from rest_framework import viewsets

from rest_hooks.models import Hook

from workflow.serializers import HookSerializer


class HookViewSet(viewsets.ModelViewSet):
    """
    Retrieve, create, update or destroy webhooks.
    """
    queryset = Hook.objects.all()
    model = Hook
    serializer_class = HookSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
