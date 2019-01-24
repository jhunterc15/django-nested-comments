from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models.query import Prefetch
from django.http import JsonResponse
from django.http.response import Http404
from django.shortcuts import render
from django.template import loader
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from .forms import CommentVersionForm
from .models import Comment, CommentVersion
from .signals import comment_changed
from .utils import InvalidCommentException, _get_target_comment, _get_or_create_tree_root, _process_node_permissions, user_has_permission, get_attr_val

import json

def get_comment(request):
    comment, previous_version = _get_target_comment(request)
    return comment, previous_version

def user_can_post_comment(request, comment):
    parent_comment = comment.parent
    tree_root = parent_comment.get_root()
    parent_object = tree_root.content_object
    return user_has_permission(request, parent_object, 'can_post_comment', comment=comment)

def is_past_max_depth(comment):
    parent_comment = comment.parent
    tree_root = parent_comment.get_root()
    
    return parent_comment.level >= tree_root.max_depth

def add_comment(comment):
    # TODO: Add the ability to override "position" (default is 'last-child')
    parent_comment = comment.parent
    return Comment.objects.insert_node(comment, parent_comment, save=True)

def lock_comment(comment):
    Comment.objects.select_for_update(nowait=True).get(pk=comment.pk)
    
def not_most_recent_version(comment, previous_version):
    return previous_version and previous_version != comment.versions.latest()

def create_new_version(request, comment):
    version_form = CommentVersionForm(request.POST)
    new_version = None
    if version_form.is_valid():
        new_version = version_form.save(commit=False)
        new_version.comment = comment
        new_version.posting_user = request.user
        new_version.save()

    return version_form, new_version

def get_template(request, comment, parent_object, tree_root, new_version, previous_version, send_signal=True):
    # The 'X_KWARGS' header is populated by settings.kwarg in comments.js
    kwargs = json.loads(request.META.get('HTTP_X_KWARGS', {}))

    # Now that the version has been saved, we fire off the appropriate signal before returning the rendered template
    if previous_version:
        if send_signal: comment_changed.send(sender=comment.__class__, comment=comment, request=request, version_saved=new_version, comment_action='edit', kwargs=kwargs)
        comment_template = get_attr_val(request, parent_object, 'single_comment_template', 'comments/comments.html')
    else:
        if send_signal: comment_changed.send(sender=comment.__class__, comment=comment, request=request, version_saved=new_version, comment_action='post', kwargs=kwargs)
        comment_template = get_attr_val(request, parent_object, 'comments_template', 'comments/comments.html')

    kwargs.update({
                   'request': request, 
                   'node': comment,
                   'nodes': [comment], # We need both because of _process_node_permissions and the fact that 'post' requires the full comments template
                   'latest_version': comment.versions.latest(), # We need this here because the latest version is not available inside the single comment template (used for edit)
                   'parent_object': parent_object,
                   'max_depth': tree_root.max_depth
                   }) 

    # Checks/assigns permissions to each node (so the template doesn't have to)
    _process_node_permissions(**kwargs)

    return comment_template, kwargs

def post_comment_form(request):
    """
    View function that handles inserting new comments via POST data (form submission)
    """
    try:
        comment, previous_version = get_comment(request)
    except InvalidCommentException, e:
        raise
    
    parent_comment = comment.parent
    tree_root = parent_comment.get_root()
    parent_object = tree_root.content_object
    if not user_can_post_comment(request, comment):
        raise Exception("User can't create comments")
    
    if is_past_max_depth(comment):
        raise Exception("Max depth reached")
    
    # If the comment object (NOT the message) hasn't been saved yet...
    if comment._state.adding == True:
       comment = add_comment(comment)
       
    # Everything has checked out, so we save the new version and return the appropriate response
    version_form, new_version = create_new_version(request, comment)
    
    return comment
    
@transaction.atomic
@require_POST
def post_comment(request, send_signal=True):
    """
    View function that handles inserting new/editing previously existing comments via Ajax
    """
    # Based on variables passed in we get the comment the user is attempting to create/edit
    try:
        comment, previous_version = get_comment(request)
    except InvalidCommentException, e:
        transaction.rollback()
        return JsonResponse({ 
            'ok': False,
            'error_message': e.message,
        })
    
    # Check if the user doesn't pass the appropriate permission check (on the parent_object)...
    # We call this on the parent comment because the comment itself may not have been saved yet (can't call .get_root on it)
    # TODO: Fix this for root comment? (no parent)
    parent_comment = comment.parent
    tree_root = parent_comment.get_root()
    parent_object = tree_root.content_object
    if not user_can_post_comment(request, comment):
        transaction.set_rollback(True)
        return JsonResponse({ 
            'ok': False,
            'error_message': "You do not have permission to post this comment.",
        })
     
    # Check to make sure we are not trying to save a comment "deeper" than we are allowed...   
    if is_past_max_depth(comment):
        transaction.set_rollback(True)
        return JsonResponse({ 
            'ok': False,
            'error_message': "You cannot respond to this comment.",
        })
    
    # If the comment object (NOT the message) hasn't been saved yet...
    if comment._state.adding == True:
       comment = add_comment(comment)
    
    # Now that we have a comment object, we get a 'lock' on it to prevent a race condition
    try:
        lock_comment(comment)
    except DatabaseError:
        transaction.set_rollback(True)
        # Someone is already trying to update this comment, so we need to return an appropriate error
        return JsonResponse({ 
            'ok': False,
            'error_message': "Someone else is currently editing this comment. Please refresh your page and try again.",
        })
    
    # Now we know we have sole access to the comment object at the moment so we need to check if we are editing the most recent version
    if not_most_recent_version(comment, previous_version):
        transaction.set_rollback(True)
        return JsonResponse({ 
            'ok': False,
            'error_message': "You are not editing the most recent version of this comment. Please refresh your page and try again.",
        })
    
    # Everything has checked out, so we save the new version and return the appropriate response
    version_form, new_version = create_new_version(request, comment)
    if version_form.is_valid():
        comment_template, kwargs = get_template(request, comment, parent_object, tree_root, new_version, previous_version, send_signal=send_signal)
        
        return JsonResponse({ 
            'ok': True,
            'html_content': loader.render_to_string(comment_template, context=kwargs)
        })
    else:
        transaction.set_rollback(True)
        return JsonResponse({ 
            'ok': False,
            'error_message': "There were errors in your submission. Please correct them and resubmit.",
        })
        
@transaction.atomic
@require_POST
def delete_comment(request):
    # Based on variables passed in we get the comment the user is attempting to create/edit
    try:
        comment, previous_version = _get_target_comment(request)
    except InvalidCommentException, e:
        transaction.set_rollback(True)
        return JsonResponse({ 
            'ok': False,
            'error_message': e.message,
        })
    
    # Check if the user doesn't pass the appropriate permission check (on the parent_object)...
    # We call this on the parent comment because the comment itself may not have been saved yet (can't call .get_root on it)
    # TODO: Fix this for root comment? (no parent)
    parent_comment = comment.parent
    tree_root = parent_comment.get_root()
    parent_object = tree_root.content_object
    if not user_has_permission(request, parent_object, 'can_delete_comment', comment=comment):
        transaction.set_rollback(True)
        return JsonResponse({ 
            'ok': False,
            'error_message': "You do not have permission to post this comment.",
        })
    
    try:
        # The 'X_KWARGS' header is populated by settings.kwarg in comments.js
        kwargs = json.loads(request.META.get('HTTP_X_KWARGS', {}))
        comment_changed.send(sender=comment.__class__, comment=comment, request=request, version_saved=None, comment_action='pre_delete', kwargs=kwargs)
        
        comment.deleted = True
        comment.save()
        for child in comment.get_children():
            child.deleted = True
            child.save()
        
        return JsonResponse({ 
            'ok': True,
        })
    except Exception, e:
        # TODO: Handle this more eloquently? Log? Probably best not to pass back raw error.
        transaction.set_rollback(True)
        return JsonResponse({ 
            'ok': False,
            'error_message': 'There was an error deleting the selected comment(s).',
        })

@transaction.atomic
@require_GET
def load_comments(request):
    """
    View function that returns the comment tree for the desired parent object.
    NOTE: This MUST be called at least once before "post_comment" or the root of the comment tree will not exist.
    """
    # TODO: Add the ability to return comment tree in JSON format.
    # First we get the root of the comment tree being requested
    try:
        tree_root, parent_object = _get_or_create_tree_root(request)
    except InvalidCommentException, e:
        return JsonResponse({ 
            'ok': False,
            'error_message': e.message,
        })
        
    # Check if the user doesn't pass the appropriate permission check (on the parent_object)...
    if not user_has_permission(request, parent_object, 'can_view_comments'):
        return JsonResponse({ 
            'ok': False,
            'error_message': "You do not have permission to view comments for this object.",
        })
        
    # Once we have our desired nodes, we tack on all of the select/prefetch related stuff
    nodes = tree_root.get_family().select_related('deleted_user_info', 'created_by', 'parent', 'content_type')\
                                  .prefetch_related(Prefetch('versions', queryset=CommentVersion.objects.order_by('-date_posted')\
                                                                                                        .select_related('posting_user', 'deleted_user_info')))
    
    comments_template = get_attr_val(request, parent_object, 'comments_template', 'comments/comments.html')
    # The 'X_KWARGS' header is populated by settings.kwarg in comments.js
    kwargs = json.loads(request.META.get('HTTP_X_KWARGS', {}))
    kwargs.update({
                   'nodes': nodes,
                   'parent_object': parent_object,
                   'max_depth': tree_root.max_depth
                   })
    
    # In the parent_object, sites can define a function called 'filter_nodes' if they wish to apply any additional filtering to the nodes queryset before it's rendered to the template.
    # Default value is the nodes tree with the deleted comments filtered out.
    nodes = get_attr_val(request, parent_object, "filter_nodes", default=nodes.filter(deleted=False), **kwargs)
    kwargs.update({"nodes": nodes, 'request': request})
    
    # Checks/assigns permissions to each node (so the template doesn't have to)
    _process_node_permissions(**kwargs)
    
    return JsonResponse({ 
        'ok': True,
        'html_content': loader.render_to_string(comments_template, context=kwargs, request=request),
        'number_of_comments': tree_root.get_descendant_count()
    })