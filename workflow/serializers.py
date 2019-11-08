import jwt
import secrets

from urllib.parse import urljoin

from django.contrib.auth import password_validation, get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.conf import settings
from django.utils.encoding import force_bytes, force_text
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.template import Template, Context

from oauth2_provider.models import AccessToken, Application, RefreshToken

from rest_framework import exceptions, serializers
from workflow import models as wfm
from workflow.email_utils import send_email, send_email_body
from workflow.models import Organization

from rest_hooks.models import Hook

User = get_user_model()


class HookSerializer(serializers.ModelSerializer):
    def validate_event(self, event):
        if event not in settings.HOOK_EVENTS:
            err_msg = "Unexpected event {}".format(event)
            raise exceptions.ValidationError(detail=err_msg, code=400)
        return event

    class Meta:
        model = Hook
        fields = '__all__'
        read_only_fields = ('user',)


class PermissionsField(serializers.DictField):
    """
    Field for representing int-value permissions as a JSON object in the format.
    For example:
    9 -> '1001' (binary representation) -> `{'create': True, 'read': False, 'update': False, 'delete': True}`
    """
    _keys = ('create', 'read', 'update', 'delete')

    def __init__(self, *args, **kwargs):
        kwargs['child'] = serializers.BooleanField()
        super().__init__(*args, **kwargs)

    def to_representation(self, value):
        permissions = list('{0:04b}'.format(value if value < 16 else 15))
        return dict(zip(self._keys, map(bool, map(int, permissions))))

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        keys = data.keys()
        if not set(keys) == set(self._keys):
            raise serializers.ValidationError("Permissions field: incorrect keys format")

        permissions = ''.join([str(int(data[key])) for key in self._keys])
        return int(permissions, 2)


class WorkflowLevel1Serializer(serializers.ModelSerializer):

    class Meta:
        model = wfm.WorkflowLevel1
        fields = '__all__'


class WorkflowLevelTypeSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source='uuid', read_only=True)

    class Meta:
        model = wfm.WorkflowLevelType
        fields = '__all__'


class WorkflowLevelStatusSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source='uuid', read_only=True)

    class Meta:
        model = wfm.WorkflowLevelStatus
        fields = '__all__'


class WorkflowLevel2Serializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source='level2_uuid', read_only=True)

    class Meta:
        model = wfm.WorkflowLevel2
        fields = '__all__'


class UUIDPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):

    def to_representation(self, value):
        return str(super().to_representation(value))


class CoreGroupSerializer(serializers.ModelSerializer):

    permissions = PermissionsField(required=False)
    organization = UUIDPrimaryKeyRelatedField(required=False,
                                              queryset=Organization.objects.all(),
                                              help_text="Related Org to associate with")

    class Meta:
        model = wfm.CoreGroup
        read_only_fields = ('uuid', 'workflowlevel1s', 'workflowlevel2s')
        fields = ('id', 'uuid', 'name', 'is_global', 'is_org_level', 'permissions', 'organization', 'workflowlevel1s',
                  'workflowlevel2s')


class CoreUserSerializer(serializers.ModelSerializer):
    """
    Default CoreUser serializer
    """
    is_active = serializers.BooleanField(required=False)
    core_groups = CoreGroupSerializer(read_only=True, many=True)
    invitation_token = serializers.CharField(required=False)

    def validate_invitation_token(self, value):
        try:
            decoded = jwt.decode(value, settings.SECRET_KEY, algorithms='HS256')
            if User.objects.filter(email=decoded['email']).exists() or decoded['email'] != self.initial_data['email']:
                raise serializers.ValidationError('Token is not valid.')
        except jwt.DecodeError:
            raise serializers.ValidationError('Token is not valid.')
        except jwt.ExpiredSignatureError:
            raise serializers.ValidationError('Token is expired.')
        return value

    class Meta:
        model = wfm.CoreUser
        fields = ('id', 'core_user_uuid', 'first_name', 'last_name', 'email', 'username', 'is_active',
                  'title', 'contact_info', 'privacy_disclaimer_accepted', 'organization', 'core_groups',
                  'invitation_token')
        read_only_fields = ('core_user_uuid', 'organization',)
        depth = 1


class CoreUserWritableSerializer(CoreUserSerializer):
    """
    Override default CoreUser serializer for writable actions (create, update, partial_update)
    """
    password = serializers.CharField(write_only=True)
    organization_name = serializers.CharField(source='organization.name')
    core_groups = serializers.PrimaryKeyRelatedField(many=True, queryset=wfm.CoreGroup.objects.all(), required=False)

    class Meta:
        model = wfm.CoreUser
        fields = CoreUserSerializer.Meta.fields + ('password', 'organization_name')
        read_only_fields = CoreUserSerializer.Meta.read_only_fields

    def create(self, validated_data):
        # get or create organization
        organization = validated_data.pop('organization')
        organization, is_new_org = wfm.Organization.objects.get_or_create(**organization)

        core_groups = validated_data.pop('core_groups', [])

        # create core user
        invitation_token = validated_data.pop('invitation_token', None)
        validated_data['is_active'] = is_new_org or bool(invitation_token)
        coreuser = wfm.CoreUser.objects.create(
            organization=organization,
            **validated_data
        )
        # set user password
        coreuser.set_password(validated_data['password'])
        coreuser.save()

        # add org admin role to the user if org is new
        if is_new_org:
            group_org_admin = wfm.CoreGroup.objects.get(organization=organization,
                                                        is_org_level=True,
                                                        permissions=wfm.PERMISSIONS_ORG_ADMIN)
            coreuser.core_groups.add(group_org_admin)

        # add requested groups to the user
        for group in core_groups:
            coreuser.core_groups.add(group)

        return coreuser


class CoreUserInvitationSerializer(serializers.Serializer):
    emails = serializers.ListField(child=serializers.EmailField(),
                                   min_length=1, max_length=10)


class CoreUserResetPasswordSerializer(serializers.Serializer):
    """Serializer for reset password request data
    """
    email = serializers.EmailField()

    def save(self, **kwargs):
        resetpass_url = urljoin(settings.FRONTEND_URL, settings.RESETPASS_CONFIRM_URL_PATH)
        resetpass_url = resetpass_url + '{uid}/{token}/'

        email = self.validated_data["email"]

        count = 0
        for user in User.objects.filter(email=email, is_active=True):
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            context = {
                'password_reset_link': resetpass_url.format(uid=uid, token=token),
                'user': user,
            }

            # get specific subj and templates for user's organization
            tpl = wfm.EmailTemplate.objects.filter(organization=user.organization,
                                                   type=wfm.TEMPLATE_RESET_PASSWORD).first()
            if not tpl:
                tpl = wfm.EmailTemplate.objects.filter(organization__name=settings.DEFAULT_ORG,
                                                       type=wfm.TEMPLATE_RESET_PASSWORD).first()
            if tpl and tpl.template:
                context = Context(context)
                text_content = Template(tpl.template).render(context)
                html_content = Template(tpl.template_html).render(context) if tpl.template_html else None
                count += send_email_body(email, tpl.subject, text_content, html_content)
                continue

            # default subject and templates
            subject = 'Reset your password'
            template_name = 'email/coreuser/password_reset.txt'
            html_template_name = 'email/coreuser/password_reset.html'
            count += send_email(email, subject, context, template_name, html_template_name)

        return count


class CoreUserResetPasswordCheckSerializer(serializers.Serializer):
    """Serializer for checking token for resetting password
    """
    uid = serializers.CharField()
    token = serializers.CharField()

    def validate(self, attrs):
        # Decode the uidb64 to uid to get User object
        try:
            uid = force_text(urlsafe_base64_decode(attrs['uid']))
            self.user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise serializers.ValidationError({'uid': ['Invalid value']})

        # Check the token
        if not default_token_generator.check_token(self.user, attrs['token']):
            raise serializers.ValidationError({'token': ['Invalid value']})

        return attrs


class CoreUserResetPasswordConfirmSerializer(CoreUserResetPasswordCheckSerializer):
    """Serializer for reset password data
    """
    new_password1 = serializers.CharField(max_length=128)
    new_password2 = serializers.CharField(max_length=128)

    def validate(self, attrs):

        attrs = super().validate(attrs)

        password1 = attrs.get('new_password1')
        password2 = attrs.get('new_password2')
        if password1 != password2:
            raise serializers.ValidationError("The two password fields didn't match.")
        password_validation.validate_password(password2, self.user)

        return attrs

    def save(self):
        self.user.set_password(self.validated_data["new_password1"])
        self.user.save()
        return self.user


class OrganizationSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source='organization_uuid', read_only=True)

    class Meta:
        model = wfm.Organization
        fields = '__all__'


class InternationalizationSerializer(serializers.ModelSerializer):

    class Meta:
        model = wfm.Internationalization
        fields = '__all__'


class WorkflowLevel2NameSerializer(serializers.ModelSerializer):

    class Meta:
        model = wfm.WorkflowLevel2
        fields = ('level2_uuid', 'name')
        read_only_fields = ('level2_uuid', )


class WorkflowLevel2SortSerializer(serializers.ModelSerializer):

    class Meta:
        model = wfm.WorkflowLevel2Sort
        fields = '__all__'
        read_only_fields = ('level2_uuid', )


class WorkflowTeamSerializer(serializers.ModelSerializer):

    class Meta:
        model = wfm.WorkflowTeam
        fields = '__all__'


class WorkflowTeamListFullSerializer(serializers.ModelSerializer):
    workflowlevel1 = WorkflowLevel1Serializer()

    class Meta:
        model = wfm.WorkflowTeam
        fields = '__all__'


class AccessTokenSerializer(serializers.ModelSerializer):
    user = CoreUserSerializer()

    class Meta:
        model = AccessToken
        fields = ('id', 'user', 'token', 'expires')


class RefreshTokenSerializer(serializers.ModelSerializer):
    access_token = AccessTokenSerializer()
    user = CoreUserSerializer()

    class Meta:
        model = RefreshToken
        fields = ('id', 'user', 'token', 'access_token', 'revoked')


class ApplicationSerializer(serializers.ModelSerializer):
    client_id = serializers.CharField(read_only=True, max_length=100)
    client_secret = serializers.CharField(read_only=True, max_length=255)

    class Meta:
        model = Application
        fields = ('id', 'authorization_grant_type', 'client_id', 'client_secret', 'client_type', 'name',
                  'redirect_uris')

    def create(self, validated_data):
        validated_data['client_id'] = secrets.token_urlsafe(75)
        validated_data['client_secret'] = secrets.token_urlsafe(190)
        return super(ApplicationSerializer, self).create(validated_data)
