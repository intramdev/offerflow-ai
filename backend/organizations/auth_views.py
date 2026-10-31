"""Session authentication endpoints for the same-origin frontend.

Login stays CSRF-protected even before a session exists (DRF only checks
authenticated sessions, so the login view enforces the token explicitly).
The token rotates on login, so clients must re-read the cookie after
authenticating before their next unsafe request.
"""

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import rotate_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True)


class CsrfView(APIView):
    """Mint the CSRF cookie the frontend sends back on unsafe methods."""

    permission_classes = []

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return Response({"detail": "CSRF cookie set."})


class LoginView(APIView):
    """Authenticate with username and password, opening a session."""

    permission_classes = []

    @method_decorator(csrf_protect)
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )
        if user is None or not user.is_active:
            return Response(
                {"detail": "Invalid credentials."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        login(request, user)
        rotate_token(request)
        return Response(_profile_payload(user))


class LogoutView(APIView):
    """Close the current session."""

    def post(self, request):
        logout(request)
        return Response({"detail": "Logged out."})


class ProfileSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150, allow_blank=True)
    last_name = serializers.CharField(max_length=150, allow_blank=True)
    email = serializers.EmailField(allow_blank=True)


def _profile_payload(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


class MeView(APIView):
    """Read or update the current session user's profile."""

    def get(self, request):
        return Response(_profile_payload(request.user))

    def patch(self, request):
        serializer = ProfileSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = request.user
        for field, value in serializer.validated_data.items():
            setattr(user, field, value)
        user.save(update_fields=["first_name", "last_name", "email"])
        return Response(_profile_payload(user))


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetRequestView(APIView):
    """Email a 30-minute reset link; unknown addresses answer identically."""

    permission_classes = []
    throttle_scope = "password_reset"

    @method_decorator(csrf_protect)
    def post(self, request):
        from django.contrib.auth import get_user_model

        from organizations.password_reset import send_reset_email

        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        user = (
            get_user_model()
            .objects.filter(email__iexact=email, is_active=True)
            .order_by("id")
            .first()
        )
        if user is not None:
            try:
                send_reset_email(user)
            except Exception:
                return Response(
                    {
                        "detail": "Could not send the reset email; "
                        "the email service is not reachable."
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
        return Response(
            {
                "detail": "If an account uses that email, "
                "a reset link is on its way."
            }
        )


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, min_length=1)


class PasswordResetConfirmView(APIView):
    """Set a new password from a live reset link (single use)."""

    permission_classes = []

    @method_decorator(csrf_protect)
    def post(self, request):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.password_validation import validate_password
        from django.contrib.auth.tokens import default_token_generator
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.utils.encoding import force_str
        from django.utils.http import urlsafe_base64_decode

        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = None
        try:
            uid = force_str(urlsafe_base64_decode(data["uid"]))
            user = get_user_model().objects.get(pk=uid, is_active=True)
        except Exception:
            user = None
        if user is None or not default_token_generator.check_token(
            user, data["token"]
        ):
            return Response(
                {"detail": "This link is invalid or expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            validate_password(data["new_password"], user=user)
        except DjangoValidationError as error:
            return Response(
                {"detail": "; ".join(error.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.set_password(data["new_password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Password changed. You can sign in now."})
