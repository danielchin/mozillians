from django.contrib import auth
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.cache import cache_page, never_cache
from django.views.decorators.http import require_POST

import commonware.log
from funfactory.urlresolvers import reverse
from tower import ugettext as _

import mozillians.phonebook.forms as forms
from mozillians.common.decorators import allow_public, allow_unvouched
from mozillians.common.middleware import LOGIN_MESSAGE, GET_VOUCHED_MESSAGE
from mozillians.groups.helpers import stringify_groups
from mozillians.groups.models import Group
from mozillians.phonebook.models import Invite
from mozillians.phonebook.utils import update_invites
from mozillians.users.managers import EMPLOYEES, MOZILLIANS, PUBLIC, PRIVILEGED
from mozillians.users.models import COUNTRIES, UserProfile
from mozillians.users.tasks import remove_from_basket_task, unindex_objects


log = commonware.log.getLogger('m.phonebook')
BAD_VOUCHER = 'Unknown Voucher'


@allow_unvouched
def login(request):
    if request.user.userprofile.is_complete:
        return redirect('phonebook:home')
    return redirect('phonebook:profile_edit')


@never_cache
@allow_public
def home(request):
    return render(request, 'phonebook/home.html')


@allow_public
@never_cache
def view_profile(request, username):
    """View a profile by username."""
    data = {}
    if (request.user.is_authenticated() and request.user.username == username):
        # own profile
        view_as = request.GET.get('view_as', 'myself')
        profile = request.user.userprofile
        if view_as == 'anonymous':
            profile = (UserProfile.objects
                       .privacy_level(PUBLIC).get(user__username=username))
        elif view_as == 'mozillian':
            profile = (UserProfile.objects
                       .privacy_level(MOZILLIANS).get(user__username=username))
        elif view_as == 'employee':
            profile = (UserProfile.objects
                       .privacy_level(EMPLOYEES).get(user__username=username))
        elif view_as == 'privileged':
            profile = (UserProfile.objects
                       .privacy_level(PRIVILEGED).get(user__username=username))
        data['privacy_mode'] = view_as
    else:
        userprofile_query = UserProfile.objects.filter(user__username=username)
        public_profile_exists = userprofile_query.public().exists()
        profile_exists = userprofile_query.exists()
        profile_complete = userprofile_query.exclude(full_name='').exists()

        if not public_profile_exists:
            if not request.user.is_authenticated():
                # you have to be authenticated to continue
                messages.warning(request, LOGIN_MESSAGE)
                return login_required(view_profile)(request, username)
            if not request.user.userprofile.is_vouched:
                # you have to be vouched to continue
                messages.error(request, GET_VOUCHED_MESSAGE)
                return redirect('phonebook:home')

        if not profile_exists or not profile_complete:
            raise Http404

        profile = UserProfile.objects.get(user__username=username)
        profile.set_instance_privacy_level(PUBLIC)
        if request.user.is_authenticated():
            profile.set_instance_privacy_level(
                request.user.userprofile.privacy_level)

        if (not profile.is_vouched
            and request.user.is_authenticated()
            and request.user.userprofile.is_vouched):
            data['vouch_form'] = (
                forms.VouchForm(initial={'vouchee': profile.pk}))

    data['shown_user'] = profile.user
    data['profile'] = profile
    return render(request, 'phonebook/profile.html', data)


@allow_unvouched
@never_cache
def edit_profile(request):
    """Edit user profile view."""
    # Don't user request.user
    user = User.objects.get(pk=request.user.id)
    profile = user.userprofile
    user_groups = stringify_groups(profile.groups.all().order_by('name'))
    user_skills = stringify_groups(profile.skills.all().order_by('name'))
    user_languages = stringify_groups(profile.languages.all().order_by('name'))

    user_form = forms.UserForm(request.POST or None, instance=user)
    new_profile = False
    form = forms.ProfileForm
    if not profile.is_complete:
        new_profile = True
        form = forms.RegisterForm

    profile_form = form(request.POST or None, request.FILES or None,
                        instance=profile, locale=request.locale,
                        initial=dict(groups=user_groups, skills=user_skills,
                                     languages=user_languages))

    if (user_form.is_valid() and profile_form.is_valid()):
        old_username = request.user.username
        user_form.save()
        profile_form.save()

        # Notify the user that their old profile URL won't work.
        if new_profile:
            update_invites(request)
            messages.info(request, _(u'Your account has been created.'))
        elif user.username != old_username:
            messages.info(request,
                          _(u'You changed your username; please note your '
                            'profile URL has also changed.'))
        return redirect(reverse('phonebook:profile_view', args=[user.username]))

    data = dict(profile_form=profile_form,
                user_form=user_form,
                user_groups=user_groups,
                my_vouches=UserProfile.objects.filter(vouched_by=profile),
                profile=request.user.userprofile,
                apps=user.apiapp_set.filter(is_active=True))

    # If there are form errors, don't send a 200 OK.
    status = 400 if (profile_form.errors or user_form.errors) else 200
    return render(request, 'phonebook/edit_profile.html', data, status=status)


@allow_unvouched
@never_cache
def confirm_delete(request):
    """Display a confirmation page asking the user if they want to
    leave.

    """
    return render(request, 'phonebook/confirm_delete.html')


@allow_unvouched
@never_cache
@require_POST
def delete(request):
    user = request.user
    unindex_objects.delay(UserProfile, [user.userprofile.id], public_index=False)
    unindex_objects.delay(UserProfile, [user.userprofile.id], public_index=True)
    remove_from_basket_task.delay(user.email, user.userprofile.basket_token)
    user.userprofile.anonymize()
    log.info('Deleting %d' % user.id)
    auth.logout(request)
    return redirect('phonebook:home')


@allow_public
def search(request):
    num_pages = 0
    limit = None
    people = []
    show_pagination = False
    form = forms.SearchForm(request.GET)
    groups = None
    curated_groups = None

    if form.is_valid():
        query = form.cleaned_data.get('q', u'')
        limit = form.cleaned_data['limit']
        include_non_vouched = form.cleaned_data['include_non_vouched']
        page = request.GET.get('page', 1)
        curated_groups = Group.get_curated()
        public = not (request.user.is_authenticated()
                      and request.user.userprofile.is_vouched)

        profiles = UserProfile.search(query, public=public,
                                      include_non_vouched=include_non_vouched)
        if not public:
            groups = Group.search(query)

        paginator = Paginator(profiles, limit)

        try:
            people = paginator.page(page)
        except PageNotAnInteger:
            people = paginator.page(1)
        except EmptyPage:
            people = paginator.page(paginator.num_pages)

        if profiles.count() == 1 and not groups:
            return redirect('phonebook:profile_view', people[0].user.username)

        if paginator.count > forms.PAGINATION_LIMIT:
            show_pagination = True
            num_pages = len(people.paginator.page_range)

    d = dict(people=people,
             search_form=form,
             limit=limit,
             show_pagination=show_pagination,
             num_pages=num_pages,
             groups=groups,
             curated_groups=curated_groups)

    if request.is_ajax():
        return render(request, 'search_ajax.html', d)

    return render(request, 'phonebook/search.html', d)


@allow_public
@cache_page(60 * 60 * 168)  # 1 week.
def search_plugin(request):
    """Render an OpenSearch Plugin."""
    return render(request, 'phonebook/search_opensearch.xml',
                  content_type='application/opensearchdescription+xml')


def invite(request):
    profile = request.user.userprofile
    invite_form = forms.InviteForm(request.POST or None,
                                   instance=Invite(inviter=profile))
    if request.method == 'POST' and invite_form.is_valid():
        invite = invite_form.save()
        invite.send(sender=profile)
        msg = _(u"%s has been invited to Mozillians. They'll receive an email "
                 "with instructions on how to join. You can "
                 "invite another Mozillian if you like." % invite.recipient)
        messages.success(request, msg)
        return redirect('phonebook:home')

    return render(request, 'phonebook/invite.html',
                  {'invite_form': invite_form})


@require_POST
def vouch(request):
    """Vouch a user."""
    form = forms.VouchForm(request.POST)

    if form.is_valid():
        p = UserProfile.objects.get(pk=form.cleaned_data.get('vouchee'))
        p.vouch(request.user.userprofile)

        # Notify the current user that they vouched successfully.
        msg = _(u'Thanks for vouching for a fellow Mozillian! '
                 'This user is now vouched!')
        messages.info(request, msg)
        return redirect('phonebook:profile_view', p.user.username)

    return HttpResponseBadRequest()


def list_mozillians_in_location(request, country, region=None, city=None):
    country = country.lower()
    country_name = COUNTRIES.get(country, country)
    queryset = UserProfile.objects.vouched().filter(country=country)
    if city:
        queryset = queryset.filter(city__iexact=city)
    if region:
        queryset = queryset.filter(region__iexact=region)

    data = {'people': queryset,
            'country_name': country_name,
            'city_name': city,
            'region_name': region}
    return render(request, 'phonebook/location-list.html', data)


@allow_unvouched
def logout(request, **kwargs):
    """Logout view that wraps Django's logout but always redirects.

    Django's contrib.auth.views logout method renders a template if
    the `next_page` argument is `None`, which we don't want. This view
    always returns an HTTP redirect instead.

    """
    return auth.views.logout(request, next_page=reverse('phonebook:home'), **kwargs)


@allow_public
def register(request):
    """Registers Users.

    Pulls out an invite code if it exists and auto validates the user
    if so. Single-purpose view.
    """
    # TODO already vouched users can be re-vouched?

    if 'code' in request.GET:
        request.session['invite-code'] = request.GET['code']
        if (request.user.is_authenticated()
            and not request.user.userprofile.is_vouched):
            update_invites(request)

    return redirect('phonebook:home')
