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
        return Response(
            {"id": user.id, "username": user.username, "email": user.email}
        )


class LogoutView(APIView):
    """Close the current session."""

    def post(self, request):
        logout(request)
        return Response({"detail": "Logged out."})


class MeView(APIView):
    """Return the current session user."""

    def get(self, request):
        user = request.user
        return Response(
            {"id": user.id, "username": user.username, "email": user.email}
        )
