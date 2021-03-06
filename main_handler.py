# Copyright (C) 2013 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Request Handler for /main endpoint."""

__author__ = 'alainv@google.com (Alain Vongsouvanh)'


import datetime
import io
import jinja2
import logging
import os
import webapp2

from google.appengine.api import memcache
from google.appengine.api import urlfetch

import httplib2
from apiclient import errors
from apiclient.http import MediaIoBaseUpload
from apiclient.http import BatchHttpRequest
from oauth2client.appengine import StorageByKeyName

from model import Credentials
from model import Food
from model import Exercise
import util


jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)))


class _BatchCallback(object):
  """Class used to track batch request responses."""

  def __init__(self):
    """Initialize a new _BatchCallbaclk object."""
    self.success = 0
    self.failure = 0

  def callback(self, request_id, response, exception):
    """Method called on each HTTP Response from a batch request.

    For more information, see
      https://developers.google.com/api-client-library/python/guide/batch
    """
    if exception is None:
      self.success += 1
    else:
      self.failure += 1
      logging.error(
          'Failed to insert item for user %s: %s', request_id, exception)


class MainHandler(webapp2.RequestHandler):
  """Request Handler for the main endpoint."""

  def _render_template(self, message=None):
    """Render the main page template."""
    template_values = {'userId': self.userid}
    if message:
      template_values['message'] = message
    # self.mirror_service is initialized in util.auth_required.
    try:
      template_values['contact'] = self.mirror_service.contacts().get(
        id='Healthy Bytes').execute()
    except errors.HttpError:
      logging.info('Unable to find Healthy Bytes contact.')

    timeline_items = self.mirror_service.timeline().list(maxResults=3).execute()
    template_values['timelineItems'] = timeline_items.get('items', [])

    subscriptions = self.mirror_service.subscriptions().list().execute()
    for subscription in subscriptions.get('items', []):
      collection = subscription.get('collection')
      if collection == 'timeline':
        template_values['timelineSubscriptionExists'] = True
      elif collection == 'locations':
        template_values['locationSubscriptionExists'] = True

    template = jinja_environment.get_template('templates/index.html')
    self.response.out.write(template.render(template_values))

  @util.auth_required
  def get(self):
    """Render the main page."""
    # Get the flash message and delete it.
    message = memcache.get(key=self.userid)
    memcache.delete(key=self.userid)
    self._render_template(message)

  @util.auth_required
  def post(self):
    """Execute the request and render the template."""
    operation = self.request.get('operation')
    # Dict of operations to easily map keys to methods.
    operations = {
        'insertItem': self._insert_item,
        'addFood': self._add_food,
        'addExercise': self._add_exercise,
        'insertContact': self._insert_contact,
        'deleteContact': self._delete_contact,
        'insertItemWithAction': self._insert_item_with_action,
        'insertItemAllUsers': self._insert_item_all_users
    }
    if operation in operations:
      message = operations[operation]()
    else:
      message = "I don't know how to " + operation
    # Store the flash message for 5 seconds.
    memcache.set(key=self.userid, value=message, time=5)
    self.redirect('/')

  def _add_food(self):
    name = self.request.get('foodName')
    calories = calc_foodcalories(name)
    image = find_image(name)
    now = datetime.datetime.now().date()
    f = Food(name = name, calories = calories, calories_left = calories, imagelink = image, time = now)
    f.put()
    self.present_food(f)

  def present_food(self, f):
    """Add a food to glass"""
    html = self.make_html(f)
    logging.info("HTML is %s"%html)
    text = 'Food: "%s" Calories: "%s"'%(f.name, f.calories)
    
    media_link = f.imagelink
    if media_link:
      if media_link.startswith('/'):
        media_link = util.get_full_url(self, media_link)
      resp = urlfetch.fetch(media_link, deadline=20)
      media = MediaIoBaseUpload(
          io.BytesIO(resp.content), mimetype='image/png', resumable=True)
    else:
      media = None

    body = {
      'notification': {'level': 'DEFAULT'},
      #'html': html,
      'text': text}

    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.timeline().insert(body=body, media_body=media).execute()
    return  'A food item has been sent to the timeline'

  def _add_exercise(self):
    name = self.request.get('exerciseName')
    burnrate = calc_burnrate(name)
    duration = int(self.request.get('exerciseDuration'))
    e = Exercise(name = name, burnrate = burnrate, duration = duration)
    e.put()
    foods = self.find_foods_worked_off(e)
    self.present_exercise(e, foods)

  def present_exercise(self, e, foods):
    """Display exercise result to glass"""
    for f in foods:
      text = 'Calories Left: "%s out of %s"'%(f.calories_left, f.calories)

      media_link = find_bitten_image(f.name)
      if media_link:
        if media_link.startswith('/'):
          media_link = util.get_full_url(self, media_link)
        resp = urlfetch.fetch(media_link, deadline=20)
        media = MediaIoBaseUpload(
            io.BytesIO(resp.content), mimetype='image/png', resumable=True)
      else:
        media = None

      body = {
        'notification': {'level': 'DEFAULT'},
        #'html': html,
        'text': text}
    
      # self.mirror_service is initialized in util.auth_required.
      self.mirror_service.timeline().insert(body=body, media_body=media).execute()
    return  'An exercise item has been sent to the timeline'

  def find_foods_worked_off(self, e):
    foods = Food.all()
    foods.filter("calories_left >", 0)
    res = []
    calories = int(calc_exercisecalories(e.burnrate, e.duration))
    for food in foods:
      res.append(food)
      if food.calories_left > calories:
        food.calories_left -= calories
        food.put()
        break
      else:
        calories -= food.calories
        food.calories_left = 0
        food.put()

    return res

  def make_html(self, f):
    #return '<div><p>Food: %s</p><p>Calories %s</p><p><img src="%s"/>'%(f.name, f.calories, f.imagelink)
    return '<img src="%s"><div class="photo-overlay"></div><section><p class="text-auto-size">Food: "%s" Calories "%s"</p></section>'%(f.imagelink, f.name, f.calories)

  def _insert_item(self):
    """Insert a timeline item."""
    logging.info('Inserting timeline item')
    body = {
        'notification': {'level': 'DEFAULT'},
        'html': "<article>\n  <section>\n    <ul class=\"text-x-small\">\n      <li>Gingerbread</li>\n      <li>Chocolate Chip Cookies</li>\n      <li>Tiramisu</li>\n      <li>Donuts</li>\n      <li>Sugar Plum Gummies</li>\n    </ul>\n  </section>\n  <footer>\n    <p>Grocery list</p>\n  </footer>\n</article>\n"
    }
    if self.request.get('html') == 'on':
      body['html'] = [self.request.get('message')]
    else:
      body['text'] = self.request.get('message')

    media_link = self.request.get('imageUrl')
    if media_link:
      if media_link.startswith('/'):
        media_link = util.get_full_url(self, media_link)
      resp = urlfetch.fetch(media_link, deadline=20)
      media = MediaIoBaseUpload(
          io.BytesIO(resp.content), mimetype='image/jpeg', resumable=True)
    else:
      media = None

    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.timeline().insert(body=body, media_body=media).execute()
    return  'A timeline item has been inserted.'

  def _insert_item_with_action(self):
    """Insert a timeline item user can reply to."""
    logging.info('Inserting timeline item')
    body = {
        'creator': {
            'displayName': 'Healthy Bytes',
            'id': 'Healthy_Bytes'
        },
        'text': 'Tell me what you had for lunch :)',
        'notification': {'level': 'DEFAULT'},
        'menuItems': [{'action': 'REPLY'}]
    }
    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.timeline().insert(body=body).execute()
    return 'A timeline item with action has been inserted.'

  def _insert_item_all_users(self):
    """Insert a timeline item to all authorized users."""
    logging.info('Inserting timeline item to all users')
    users = Credentials.all()
    total_users = users.count()

    if total_users > 10:
      return 'Total user count is %d. Aborting broadcast to save your quota' % (
          total_users)
    body = {
        'text': 'Hello Everyone!',
        'notification': {'level': 'DEFAULT'}
    }

    batch_responses = _BatchCallback()
    batch = BatchHttpRequest(callback=batch_responses.callback)
    for user in users:
      creds = StorageByKeyName(
          Credentials, user.key().name(), 'credentials').get()
      mirror_service = util.create_service('mirror', 'v1', creds)
      batch.add(
          mirror_service.timeline().insert(body=body),
          request_id=user.key().name())

    batch.execute(httplib2.Http())
    return 'Successfully sent cards to %d users (%d failed).' % (
        batch_responses.success, batch_responses.failure)

  def _insert_contact(self):
    """Insert a new Contact."""
    logging.info('Inserting contact')
    name = self.request.get('name')
    image_url = self.request.get('imageUrl')
    if not name or not image_url:
      return 'Must specify imageUrl and name to insert contact'
    else:
      if image_url.startswith('/'):
        image_url = util.get_full_url(self, image_url)
      body = {
          'id': name,
          'displayName': name,
          'imageUrls': [image_url]
      }
      # self.mirror_service is initialized in util.auth_required.
      self.mirror_service.contacts().insert(body=body).execute()
      return 'Inserted contact: ' + name

  def _delete_contact(self):
    """Delete a Contact."""
    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.contacts().delete(
        id=self.request.get('id')).execute()
    return 'Contact has been deleted.'


MAIN_ROUTES = [
    ('/', MainHandler)
]
def calc_foodcalories(name):
  calorieMap = {'chickenlegs': 264,
                'cookies': 132,
                'pancakes': 175,
                'platter': 80,
                'riceandveggies': 200,
                'sandwich': 46,
                'spaghetti': 400,
                'tacos': 450,
                'apple': 95
                }
  return calorieMap.get(name, 1000)

def calc_burnrate(name):
  burnrateMap = {'walking': 4.5,
                  'running': 9.5,
                  'bicycling': 6.0,
                  'rowing': 9.5,
                  'swimming': 8.0
                }
  return burnrateMap.get(name, 1.0)

def calc_exercisecalories(burnrate, duration):
  calories = burnrate * duration
  return calories

def find_image(name):
  return "/static/images/%s.png"%name

def find_bitten_image(name):
  return "/static/images/%s-bite.png"%name
