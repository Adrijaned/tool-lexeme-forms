import copy
import flask
import jinja2
import json
import mwapi
import mwoauth
import os
import random
import re
import requests
import requests_oauthlib
import string
import toolforge
import yaml
from templates import templates
from translations import translations

app = flask.Flask(__name__)

app.before_request(toolforge.redirect_to_https)

toolforge.set_user_agent('lexeme-forms', email='mail@lucaswerkmeister.de')
user_agent = requests.utils.default_user_agent()

__dir__ = os.path.dirname(__file__)
try:
    with open(os.path.join(__dir__, 'config.yaml')) as config_file:
        app.config.update(yaml.safe_load(config_file))
        consumer_token = mwoauth.ConsumerToken(app.config['oauth']['consumer_key'], app.config['oauth']['consumer_secret'])
except FileNotFoundError:
    print('config.yaml file not found, assuming local development setup')
    app.secret_key = 'fake'

@app.template_filter()
@jinja2.contextfilter
def form2input(context, form, first=False):
    example = form['example']
    match = re.match(r'^(.*)\[(.*)\](.*)$', example)
    if match:
        (prefix, placeholder, suffix) = match.groups()
        return (flask.Markup.escape(prefix) +
                flask.Markup(r'<input type="text" name="form_representation" placeholder="') +
                flask.Markup.escape(placeholder) +
                flask.Markup(r'"') +
                flask.Markup(r' pattern="[^/]+(?:/[^/]+)*"') +
                (flask.Markup(r' required') if not context['advanced'] else flask.Markup('')) +
                (flask.Markup(r' autofocus') if first else flask.Markup('')) +
                (flask.Markup(r' value="') + flask.Markup.escape(form['value']) + flask.Markup(r'"') if 'value' in form else flask.Markup('')) +
                flask.Markup(r'>') +
                flask.Markup.escape(suffix))
    else:
        raise Exception('Invalid template: missing [placeholder]: ' + example)

@app.template_filter()
def render_duplicates(duplicates, language_code):
    return flask.render_template(
        'duplicates.html',
        duplicates=duplicates,
        translations=translations[language_code],
    )

@app.template_global()
def csrf_token():
    if '_csrf_token' not in flask.session:
        flask.session['_csrf_token'] = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(64))
    return flask.session['_csrf_token']

@app.template_global()
def template_group(template):
    group = template['language_code']
    if 'test' in template:
        group += ', test.wikidata.org'
    return group

@app.template_filter()
def user_link(user_name):
    return (flask.Markup(r'<a href="https://www.wikidata.org/wiki/User:') +
            flask.Markup.escape(user_name.replace(' ', '_')) +
            flask.Markup(r'">') +
            flask.Markup(r'<bdi>') +
            flask.Markup.escape(user_name) +
            flask.Markup(r'</bdi>') +
            flask.Markup(r'</a>'))

@app.template_global()
def render_oauth_username():
    identity = identify()
    if identity is None:
        return flask.Markup(r'')
    return (flask.Markup(r'<span class="navbar-text">Logged in as ') +
            user_link(identity['username']) +
            flask.Markup(r'</span>'))

@app.route('/')
def index():
    return flask.render_template(
        'index.html',
        templates=templates,
    )

@app.route('/template/<template_name>/', methods=['GET', 'POST'])
def process_template(template_name):
    return process_template_advanced(template_name=template_name, advanced=False)

@app.route('/template/<template_name>/advanced/', methods=['GET', 'POST'])
def process_template_advanced(template_name, advanced=True):
    response = if_no_such_template_redirect(template_name)
    if response:
        return response

    response = if_needs_oauth_redirect()
    if response:
        return response

    template = templates[template_name]

    if flask.request.method == 'POST':
        form_data = flask.request.form

        response = if_has_duplicates_redirect(template, template_name, advanced, form_data)
        if response:
            return response

        response = if_needs_csrf_redirect(template, template_name, advanced, form_data)
        if response:
            return response

        lexeme_data = build_lexeme(template, form_data)

        if 'oauth' in app.config:
            return submit_lexeme(template, lexeme_data)
        else:
            return flask.Response(json.dumps(lexeme_data), mimetype='application/json')
    else:
        return flask.render_template(
            'template.html',
            template=template,
            template_name=template_name,
            translations=translations[template['language_code']],
            advanced=advanced,
        )

def if_no_such_template_redirect(template_name):
    if template_name not in templates:
        return flask.render_template(
            'no-such-template.html',
            template_name=template_name,
        )
    else:
        return None

def if_needs_oauth_redirect():
    if 'oauth' in app.config and 'oauth_access_token' not in flask.session:
        (redirect, request_token) = mwoauth.initiate('https://www.wikidata.org/w/index.php', consumer_token, user_agent=user_agent)
        flask.session['oauth_request_token'] = dict(zip(request_token._fields, request_token))
        flask.session['oauth_redirect_target'] = flask.url_for(flask.request.endpoint, **flask.request.view_args)
        return flask.redirect(redirect)
    else:
        return None

@app.route('/oauth/callback')
def oauth_callback():
    access_token = mwoauth.complete('https://www.wikidata.org/w/index.php', consumer_token, mwoauth.RequestToken(**flask.session['oauth_request_token']), flask.request.query_string, user_agent=user_agent)
    flask.session['oauth_access_token'] = dict(zip(access_token._fields, access_token))
    return flask.redirect(flask.session['oauth_redirect_target'])

def if_has_duplicates_redirect(template, template_name, advanced, form_data):
    if 'no_duplicate' in form_data:
        return None
    if 'lexeme_id' in form_data and form_data['lexeme_id']:
        return None

    duplicates = find_duplicates(template, form_data)
    if duplicates:
        return flask.render_template(
            'template.html',
            template=add_form_data_to_template(form_data, template),
            template_name=template_name,
            translations=translations[template['language_code']],
            advanced=advanced,
            duplicates=duplicates,
        )
    else:
        return None

def find_duplicates(template, form_data):
    wiki = 'test' if 'test' in template else 'www'
    language_code = template['language_code']
    lemma = get_lemma(form_data)
    if lemma:
        return get_duplicates(wiki, language_code, lemma)
    else:
        flask.abort(400)

def get_lemma(form_data):
    for form_representation in form_data.getlist('form_representation'):
        for form_representation_variant in form_representation.split('/'):
            if form_representation_variant is not '':
                return form_representation_variant
    return None

@app.route('/api/v1/duplicates/<any(www,test):wiki>/<language_code>/<path:lemma>')
def get_duplicates_api(wiki, language_code, lemma):
    matches = get_duplicates(wiki, language_code, lemma)
    if not matches:
        return flask.Response(status=204)
    if flask.request.accept_mimetypes.accept_html:
        return render_duplicates(matches, language_code)
    else:
        return flask.Response(json.dumps(matches), mimetype='application/json')

def get_duplicates(wiki, language_code, lemma):
    host = 'https://' + wiki + '.wikidata.org'
    session = mwapi.Session(
        host,
        user_agent=user_agent,
    )

    response = session.get(
        action='wbsearchentities',
        search=lemma,
        language=language_code,
        uselang=language_code, # for the result descriptions
        type='lexeme',
        limit=50,
    )
    matches = []
    for result in response['search']:
        if result['label'] == lemma and result['match']['language'] == language_code:
            matches.append({'id': result['id'], 'uri': result['concepturi'], 'label': result['label'], 'description': result['description']})

    return matches

@app.route('/api/v1/no_duplicate/<language_code>')
@app.template_global()
def render_no_duplicate(language_code):
    return flask.render_template(
        'no_duplicate.html',
        translations=translations[language_code]
    )

def add_form_data_to_template(form_data, template):
    template = copy.deepcopy(template)
    for (form_representation, form) in zip(form_data.getlist('form_representation'), template['forms']):
        form['value'] = form_representation
    if 'lexeme_id' in form_data:
        template['lexeme_id'] = form_data['lexeme_id']
    return template

def if_needs_csrf_redirect(template, template_name, advanced, form_data):
    token = flask.session.pop('_csrf_token', None)
    if not token or token != form_data.get('_csrf_token'):
        return flask.render_template(
            'template.html',
            template=add_form_data_to_template(form_data, template),
            template_name=template_name,
            translations=translations[template['language_code']],
            advanced=advanced,
            csrf_error=True,
        )
    else:
        return None

def build_lexeme(template, form_data):
    lang = template['language_code']
    forms = []
    form_representations = form_data.getlist('form_representation')
    for form_representation, form in zip(form_representations, template['forms']):
        if not form_representation:
            continue
        for form_representation_variant in form_representation.split('/'):
            if not form_representation_variant:
                flask.abort(400)
            forms.append({
                'add': '',
                'representations': {lang: {'language': lang, 'value': form_representation_variant}},
                'grammaticalFeatures': form['grammatical_features_item_ids'],
                'claims': form.get('claims', {})
            })
    lexeme_data = {
        'type': 'lexeme',
        'forms': forms,
    }
    lexeme_id = form_data.get('lexeme_id', '')
    if lexeme_id:
        lexeme_data['id'] = lexeme_id
    else:
        lemma = get_lemma(form_data)
        if lemma is None:
            flask.abort(400)
        lexeme_data.update({
            'lemmas': {lang: {'language': lang, 'value': lemma}},
            'language': template['language_item_id'],
            'lexicalCategory': template['lexical_category_item_id'],
            'claims': template.get('claims', {}),
        })
    return lexeme_data

def submit_lexeme(template, lexeme_data):
    if 'test' in template:
        host = 'https://test.wikidata.org'
    else:
        host = 'https://www.wikidata.org'
    session = mwapi.Session(
        host=host,
        auth=generate_auth(),
        user_agent=user_agent,
    )

    token = session.get(action='query', meta='tokens')['query']['tokens']['csrftoken']
    selector = {'id': lexeme_data['id']} if 'id' in lexeme_data else {'new': 'lexeme'}
    response = session.post(
        action='wbeditentity',
        data=json.dumps(lexeme_data),
        token=token,
        **selector
    )
    lexeme_id = response['entity']['id']
    revid = response['entity']['lastrevid']

    # wbeditentity does not support statements for forms yet, add them afterwards (T194732)
    # TODO once wbeditentity *does* support it, this will create duplicate statements :(
    if 'id' not in lexeme_data:
        for form_index, form_data in enumerate(lexeme_data['forms']):
            if form_data['claims']:
                for statementlist in form_data['claims'].values():
                    for statement in statementlist:
                        mainsnak = statement['mainsnak']
                        session.post(
                            action='wbcreateclaim',
                            entity=lexeme_id + '-F' + str(form_index + 1),
                            snaktype=mainsnak['snaktype'],
                            property=mainsnak['property'],
                            value=json.dumps(mainsnak['datavalue']['value']),
                            token=token,
                            baserevid=revid,
                        )
    else:
        # we don’t know which form IDs to edit on the existing lexeme :(
        # TODO inform the user
        pass

    return flask.redirect(host + '/entity/' + lexeme_id, code=303)

def generate_auth():
    access_token = mwoauth.AccessToken(**flask.session['oauth_access_token'])
    return requests_oauthlib.OAuth1(
        client_key=consumer_token.key,
        client_secret=consumer_token.secret,
        resource_owner_key=access_token.key,
        resource_owner_secret=access_token.secret,
    )

def identify():
    if 'oauth_access_token' not in flask.session:
        return None
    access_token = mwoauth.AccessToken(**flask.session['oauth_access_token'])
    return mwoauth.identify(
        'https://www.wikidata.org/w/index.php',
        consumer_token,
        access_token,
    )
