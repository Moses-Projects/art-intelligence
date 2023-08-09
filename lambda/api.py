import json
import os
import random
import re
import requests
import sys

from duckduckgo_search import DDGS
import wikipedia

sys.path.append('/opt')

import moses_common.__init__ as common
import moses_common.api_gateway
import moses_common.dynamodb
import moses_common.s3
import moses_common.timer
import moses_common.ui
import moses_common.visual_artists as visual_artists


ui = moses_common.ui.Interface(use_slack_format=True, usage_message="""
Select an image from the database.
  api.py
  
  Options:
    -h, --help                  This help screen.
    -n, --dry_run               Run without updating anything.
    -v, --verbose               More output.
    -x, --extra_verbose         Even more output.
""")

log_level = 6
dry_run = False


def read_image_records(table):
	timer = moses_common.timer.Timer('Loaded index')
	records = table.get_keys(['nsfw', 'score', 'orientation', 'query-artist', 'query-subject', 'query-style'])
	ui.warning(timer.stop())
	for record in records:
		record['id'] = common.get_epoch(record['create_time'])
	return records

images_table = moses_common.dynamodb.Table('artintelligence.gallery-images', log_level=log_level, dry_run=dry_run)
image_records = read_image_records(images_table)
image_timer = moses_common.timer.Refresh()

artist_list_location = '/tmp'
if common.is_local():
	artist_list_location=os.environ['HOME']
collective = visual_artists.Collective(artist_list_location=artist_list_location, log_level=log_level, dry_run=dry_run)


def handler(event, context):
	global log_level
	global dry_run
	global image_records
	
	if image_timer.refresh(minutes=10):
		image_records = read_image_records(images_table)
	
	api = moses_common.api_gateway.Request(event, log_level=log_level, dry_run=dry_run)
	
	path = api.parse_path()
	
	method = api.method
	
	query, metadata = api.process_query()
	body = api.body
	
	output = {}
	if len(path) >= 1:
		action = path.pop(0)
		if action == 'get':
			if method == 'POST':
				output = get_image(body)
			else:
				output = { "status": 405, "error": "Method not allowed" }
		elif action == 'get_latest':
			if method == 'POST':
				output = get_latest(body)
			else:
				output = { "status": 405, "error": "Method not allowed" }
		elif action == 'get_artists':
			if method == 'GET':
				output = get_artists()
			else:
				output = { "status": 405, "error": "Method not allowed" }
		elif action == 'get_artist':
			if method == 'POST':
				output = get_artist(body)
			else:
				output = { "status": 405, "error": "Method not allowed" }
		elif action == 'get_search_results':
			if method == 'POST':
				output = get_search_results(body)
			else:
				output = { "status": 405, "error": "Method not allowed" }
		elif action == 'counts':
			if method == 'GET':
				artists_table = moses_common.dynamodb.Table('artintelligence.gallery-artists')
				images = 0
				fails = 0
				for record in image_records:
					if 'nsfw' in record and record['nsfw']:
						continue
					if 'score' in record and record['score'] == 1:
						fails += 1
						continue
					if 'score' in record and record['score'] < 3:
						continue
					images += 1
				output = {
					"images": images,
					"artists": artists_table.item_count,
					"fails": fails
				}
			else:
				output = { "status": 405, "error": "Method not allowed" }
		elif action == 'set_score':
			if method == 'PUT':
				if common.get_environment() == 'dev':
					output = set_score(body)
				else:
					output = { "status": 403, "error": "Forbidden" }
			else:
				output = { "status": 405, "error": "Method not allowed" }
		else:
			return { "statusCode": 500, "body": "Invalid path" }
	else:
		return { "statusCode": 500, "body": "Invalid path" }
	
	response = {
		"statusCode": 200,
		"body": common.make_json(output)
	}
	
# 	print("response {}: {}".format(type(response), response))
	return response


def get_artists():
	artist_records = collective.get_artists()
	artist_list = []
	for artist in artist_records:
		artist_list.append({
			"id": artist.id,
			"name": artist.name,
			"sort_name": artist.sort_name
		})
	return {
		"status": "success",
		"artists": artist_list,
		"total": len(artist_list)
	}

def get_artist(body):
	if 'artist' not in body:
		return {
			"status": "error",
			"message": "Missing 'artist' argument."
		}
	artist = collective.get_artist(body['artist'])
	artist_info = {
		"status": "success",
		"artist": {
			"id": artist.id,
			"name": artist.name,
			"categories": artist.categories
		}
	}
	page = get_wikipedia_page(artist)
	if page:
		artist_info['artist']['wikipedia_title'] = page.title
		artist_info['artist']['wikipedia_summary'] = page.summary
		artist_info['artist']['wikipedia_url'] = page.url
	
	return artist_info

def get_wikipedia_page(artist):
	try:
		page = wikipedia.page(artist.name, auto_suggest=False)
	except:
		page = None
	if not page:
		try:
			titles = wikipedia.search(artist.name)
			page = wikipedia.page(titles[0], auto_suggest=False)
		except ValueError as e:
			page = None
	return page


def get_search_results(body):
	if 'artist' not in body:
		return {
			"status": "error",
			"message": "Missing 'artist' argument."
		}
	artist = collective.get_artist(body['artist'])
	art_forms = []
	for forms in artist.art_forms:
		art_forms.append(forms['name'] + 's')
	if not art_forms:
		art_forms = ['paintings']
	keywords = ' and '.join(art_forms) + ' by ' + artist.name
	
	limit = common.convert_to_int(body.get('limit')) or 8
	results_list = []
	with DDGS() as ddgs:
		ddgs_images_gen = ddgs.images(
			keywords,
			region="us-en",
			safesearch="Off",
			size=None,
			type_image=None,
			layout=None,
			license_image=None,
		)
		cnt = 0
		for r in ddgs_images_gen:
			if cnt >= limit:
				break
			if re.search(r'\.explicit\.bing', r['thumbnail']):
				continue
			results_list.append(r)
			cnt += 1
	return {
		"status": "success",
		"images": results_list,
		"url": "https://duckduckgo.com/?" + common.url_encode({
			"iax": "images",
			"ia": "images",
			"q": keywords
		}),
		"total": len(results_list)
	}
	

def curate(body):
	final_records = []
	if 'search' in body and body['search']:
		search = body['search'].strip()
		if common.is_int(search) and len(search) == 10:
			print("id search")
			for record in image_records:
				if record['id'] == common.convert_to_int(search):
					final_records.append(record)
			return final_records
		elif re.match(r'\S+\.png$', search):
			print("filename search")
			for record in image_records:
				if record['filename'] == search:
					return [record]
			return []
	
	has_nsfw = False
	if 'nsfw' in body:
		has_nsfw = True
	get_nsfw = common.convert_to_bool(body.get('nsfw')) or False
	
	exact_score = common.convert_to_int(body.get('exact_score')) or None
	if body.get('exact_score') == 'no_score':
		exact_score = 'no_score'
	score = common.convert_to_int(body.get('score')) or None
	
	artist = body.get('artist')
	
	# Load all keys
	for record in image_records:
		include = True
		
		if body.get('search') or body.get('artist') or body.get('orientation'):
			include = False
		
		if body.get('search'):
			re_match = re.compile(r'\b{}\b'.format(common.normalize(body['search'])), re.IGNORECASE)
			if re.search(re_match, common.normalize(record.get('query-artist', ''))):
				include = True
			elif re.search(re_match, common.normalize(record.get('query-subject', ''))):
				include = True
			elif re.search(re_match, common.normalize(record.get('query-style', ''))):
				include = True
		
		if body.get('artist'):
			re_match = re.compile(r'\b{}\b'.format(common.normalize(body['artist'])), re.IGNORECASE)
			if re.search(re_match, common.normalize(record.get('query-artist', ''))):
				include = True
		
		if body.get('orientation'):
			if common.normalize(body['orientation']) == record.get('orientation', ''):
				include = True
		
		if has_nsfw:
			if not get_nsfw and record.get('nsfw'):
				include = False
			if get_nsfw and not record.get('nsfw'):
				include = False
		
		if exact_score == 'no_score':
			if 'score' in record:
				include = False
		elif exact_score:
			if exact_score != record.get('score', 0):
				include = False
		elif score and record.get('score') and score > record['score']:
			include = False
		
		if include:
			final_records.append(record)
		
	return final_records
	

def get_image(body):
	final_records = curate(body)
	if not final_records:
		return {
			"status": "success",
			"images": [],
			"total": 0
		}
	
	records = []
	limit = 1
	if 'limit' in body and common.is_int(body['limit']):
		limit = common.convert_to_int(body['limit'])
	
	records = []
	for i in range(limit):
		# Pick key at random
		index = random.randrange(len(final_records))
		record = get_image_record(final_records[index]['filename'], final_records[index]['create_time'])
		
# 		record['url'] = get_presigned_url(record['filename'])
		
		records.append(record)
	
	return {
		"status": "success",
		"images": records,
		"total": len(final_records)
	}


def get_latest(body):
	final_records = curate(body)
	if not final_records:
		return {
			"status": "success",
			"images": [],
			"offset": 0,
			"total": 0
		}
	
	# Get last record
	offset = common.convert_to_int(body.get('offset'))
	if offset > len(final_records):
		offset = 1
	if offset <= 0:
		offset = len(final_records)
	
	limit = common.convert_to_int(body.get('limit')) or 1
	if len(final_records) - offset < limit:
		limit = len(final_records) - offset + 1
	
	records = []
	for i in range(offset, offset+limit):
		index = -1 * i
		record = get_image_record(final_records[index]['filename'], final_records[index]['create_time'])
		record['offset'] = offset
		
# 		record['url'] = get_presigned_url(record['filename'])
		records.append(record)
	
	return {
		"status": "success",
		"images": records,
		"offset": offset,
		"total": len(final_records)
	}

def get_image_record(filename, create_time):
	record = images_table.get_item(filename, create_time)
	record['id'] = common.get_epoch(record['create_time'])
	return record

def get_presigned_url(filename):
	bucket_name = 'artintelligence.gallery'
	object_name = f"images/{filename}"
	bucket = moses_common.s3.Bucket(bucket_name)
	file = moses_common.s3.Object(bucket, object_name)
	return file.get_presigned_url(expiration_time=3600)

def set_score(body):
	global image_records
	for field in ['filename', 'create_time']:
		if field not in body or not body[field]:
			return {
				"status": "error",
				"message": "Missing 'filename' and 'create_time' arguments."
			}
	record = None
	for image in image_records:
		if image['filename'] == body['filename']:
			record = image
			break
	if not record:
		return {
			"status": "error",
			"message": f"No records matching '{image['filename']}'."
		}
		
	
	data = {
		"filename": body['filename'],
		"create_time": body['create_time'],
		"nsfw": False
	}
	
	record['nsfw'] = False
	if 'nsfw' in body and body['nsfw']:
		data['nsfw'] = True
		record['nsfw'] = True
	if common.is_int(body.get('score')):
		data['score'] = common.convert_to_int(body['score']);
		record['score'] = data['score']
	
	success = images_table.update_item(data)
	
	if not success:
		return {
			"status": "error",
			"message": "Failed to save changes to database."
		}
	image_records = read_image_records(images_table)
	return {
		"status": "success",
		"image": record
	}




