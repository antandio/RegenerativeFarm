from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User
@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("email","is_staff","is_active")
    ordering = ("email",)
    fieldsets = ((None, {"fields": ("email","password")}), ("Jogok", {"fields": ("is_active","is_staff","is_superuser")}))
    add_fieldsets = ((None, {"fields": ("email","password1","password2")}),)
    search_fields = ("email",)
