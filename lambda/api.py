import json
import os
import random
import re
import requests
import sys

from duckduckgo_search import DDGS
from boto3 import client as boto3_client
lambda_client = boto3_client('lambda', region_name="us-west-2",)

sys.path.append('/opt')

import moses_common.__init__ as common
import moses_common.api_gateway
import moses_common.collective
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

collective = moses_common.collective.Collective(log_level=log_level, dry_run=dry_run)


def read_image_records(table):
	timer = moses_common.timer.Timer('Loaded index')
	records = table.get_keys(['nsfw', 'score', 'aspect_ratio', 'query-artist_name', 'query-subject', 'query-style'])
	ui.warning(timer.stop())
	for record in records:
		record['id'] = common.get_epoch(record['create_time'] + '+00:00')
		if 'query-artist_id' not in record and 'query-artist_name' in record:
			record['query-artist_id'] = common.convert_to_snakecase(common.normalize(record['query-artist_name'], strip_single_chars=False))
		# SD 1.5 Checkpoints
		if record['id'] <= 1690216293:
			record['version'] = 1
		# SDXL Beta + GPT
		elif record['id'] <= 1691577582:
			record['version'] = 2
		# SDXL 1.0 + GPT
		elif record['id'] <= 1693321367:
			record['version'] = 3
		# SDXL 1.0
		else:
			record['version'] = 4
	
	collective.images_were_read()
	return records

images_table = moses_common.dynamodb.Table('artintelligence.gallery-images', log_level=log_level, dry_run=dry_run)
image_records = read_image_records(images_table)
image_timer = moses_common.timer.Refresh()

artist_list_location = '/tmp'
if common.is_local():
	artist_list_location=os.environ['HOME']
# collective = visual_artists.Collective(artist_list_location=artist_list_location, log_level=log_level, dry_run=dry_run)

def handler(event, context):
	global log_level
	global dry_run
	global image_records
	
	if collective.images_were_updated():
		image_records = read_image_records(images_table)
	
	api = moses_common.api_gateway.Request(event, log_level=7, dry_run=dry_run)
	
	path = api.parse_path()
	
	method = api.method
	
	if 'Authorization' in api.headers:
		access_token = api.headers.get('Authorization')
		user_info = get_user_info(access_token)
		print("user_info {}: {}".format(type(user_info), user_info))
	
	query, metadata = api.process_query()
	body = api.body
	
	output = {}
	if path:
		action = path.pop(0)
		if action == 'get':
			if path:
				image_id = path.pop(0)
				if re.match(r'^(latest|\d{10})$', image_id):
					output = get_image(image_id, body)
				else:
					output = { "status": 403, "error": "Forbidden" }
			elif method == 'POST':
				output = get_random(body)
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
		elif action == 'get_genre_list':
			if method == 'GET':
				output = get_genre_list()
			else:
				output = { "status": 405, "error": "Method not allowed" }
		elif action == 'get_genres':
			if method == 'POST':
				output = get_genres(body)
			else:
				output = { "status": 405, "error": "Method not allowed" }
		elif action == 'save_genre':
			if method == 'POST' or method == 'PUT':
				if common.get_environment() == 'dev':
					output = save_genre(body)
				else:
					output = { "status": 403, "error": "Forbidden" }
			else:
				output = { "status": 405, "error": "Method not allowed" }
		elif action == 'delete_genre':
			if method == 'DELETE':
				if common.get_environment() == 'dev':
					output = delete_genre(body)
				else:
					output = { "status": 403, "error": "Forbidden" }
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
		elif action == 'generate':
			if method == 'POST':
				if common.get_environment() == 'dev':
					output = generate(body)
				else:
					output = { "status": 403, "error": "Forbidden" }
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
	if log_level >= 6:
# 		del output['image']['artist']
		ui.body(f"output: {output}")
	
# 	print("response {}: {}".format(type(response), response))
	return response


def get_artists():
	artist_list = []
	for artist in collective.artists:
		name_parts = re.split(r', ', artist.sort_name);
		name_parts.append(name_parts.pop(0))
		artist_name = common.normalize(' '.join(name_parts), False)
		artist_list.append({
			"id": artist.id,
			"searchable_name": artist_name,
			"sort_name": artist.sort_name
		})
	return {
		"status": "success",
		"artists": artist_list,
		"total": len(artist_list)
	}

def get_artist(body):
	artist = None
	if 'artist_id' in body:
		artist = collective.get_artist_by_id(body['artist_id'])
	elif 'artist' in body:
		artist = collective.get_artist_by_name(body['artist'])
	else:
		return error("Missing 'artist_id' argument")
	
	if not artist:
		return error("Artist '{}{}' not found".format(body.get('artist_id'), body.get('artist')))
	artist_info = {
		"status": "success",
		"artist": artist.data
	}
	
	return artist_info

def get_genre_list():
	genre_list = collective.get_genre_list()
	return {
		"status": "success",
		"artists": genre_list,
		"total": len(genre_list)
	}

def get_genres(body):
	if 'artist_id' not in body:
		return error("Missing 'artist_id' argument")
	artist = collective.get_artist_by_id(body['artist_id'])
	if not artist:
		return error(f"Artist '{artist_id}' not found")
	return {
		"status": "success",
		"genres": artist.genre_data,
		"total": len(artist.genres)
	}

def save_genre(body):
	if 'artist_id' not in body:
		return error("Missing 'artist_id' argument")
	if 'name' not in body:
		return error("Missing 'name' argument")
	
	artist = collective.get_artist_by_id(body['artist_id'])
	if not artist:
		return error(f"Artist '{artist_id}' not found")
	
	genre = moses_common.collective.Genre(artist, body, log_level=log_level, dry_run=dry_run)
	success = genre.save()
	if not success:
		return error("Failed to save genre")
	
	return {
		"status": "success"
	}

def delete_genre(body):
	if 'artist_id' not in body:
		return error("Missing 'artist_id' argument")
	if 'name' not in body:
		return error("Missing 'name' argument")
	
	artist = collective.get_artist_by_id(body['artist_id'])
	if not artist:
		return error(f"Artist '{artist_id}' not found")
	
	genre = moses_common.collective.Genre(artist, body, log_level=log_level, dry_run=dry_run)
	success = genre.delete()
	if not success:
		return error("Failed to delete genre")
	
	return {
		"status": "success"
	}

def get_search_results(body):
	artist = None
	if 'artist_id' in body:
		artist = collective.get_artist_by_id(body['artist_id'])
	elif 'artist' in body:
		artist = collective.get_artist_by_name(body['artist'])
	else:
		return error("Missing 'artist_id' argument")
	
	keywords = 'artwork by ' + artist.name
	
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
		"artist": {
			"id": artist.id,
			"name": artist.name
		},
		"images": results_list,
		"url": "https://duckduckgo.com/?" + common.url_encode({
			"iax": "images",
			"ia": "images",
			"q": keywords
		}),
		"total": len(results_list)
	}
	

def curate(body, image_id=None):
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
	
	min_aspect_ratio = 0.1
	max_aspect_ratio = 10.0
	if 'min_aspect_ratio' in body:
		min_aspect_ratio = common.convert_to_float(body.get('min_aspect_ratio')) or 0.1
	if 'max_aspect_ratio' in body:
		max_aspect_ratio = common.convert_to_float(body.get('max_aspect_ratio')) or 10.0
	if body.get('orientation') == 'portrait':
		min_aspect_ratio = 0.1
		max_aspect_ratio = 0.9
	elif body.get('orientation') == 'landscape':
		min_aspect_ratio = 1.1
		max_aspect_ratio = 10.0
	elif body.get('orientation') == 'square':
		min_aspect_ratio = 1.0
		max_aspect_ratio = 1.0
	
	has_nsfw = False
	if 'nsfw' in body:
		has_nsfw = True
	get_nsfw = common.convert_to_bool(body.get('nsfw')) or False
	
	exact_score = common.convert_to_int(body.get('exact_score')) or None
	if body.get('exact_score') == 'no_score':
		exact_score = 'no_score'
	score = common.convert_to_int(body.get('score')) or None
	
	exact_version = common.convert_to_int(body.get('exact_version')) or None
	version = common.convert_to_int(body.get('version')) or None
	
	# Load all keys
	for record in image_records:
		include = True
		log = []
		
		if body.get('search') or body.get('artist') or body.get('artist_id'):
			include = False
		
		if body.get('search'):
			log.append('search')
			re_match = re.compile(r'\b{}\b'.format(common.normalize(body['search'])), re.IGNORECASE)
			if re.search(re_match, common.normalize(record.get('query-artist_name', ''))):
				log.append('  Match on query-artist_name')
				include = True
			elif re.search(re_match, common.normalize(record.get('query-subject', ''))):
				log.append('  Match on query-subject')
				include = True
			elif re.search(re_match, common.normalize(record.get('query-style', ''))):
				log.append('  Match on query-style')
				include = True
		
		if body.get('artist_id'):
			log.append('artist_id')
			if body['artist_id'] == record.get('query-artist_id', ''):
				log.append('  Match on query-artist_id')
				include = True
		
		if body.get('artist'):
			log.append('artist')
			re_match = re.compile(r'\b{}\b'.format(common.normalize(body['artist'])), re.IGNORECASE)
			if re.search(re_match, common.normalize(record.get('query-artist_name', ''))):
				log.append('  Match on query-artist_name')
				include = True
		
		
		if record['aspect_ratio'] > max_aspect_ratio or record['aspect_ratio'] < min_aspect_ratio:
			log.append('Filter out on aspect ratio')
			include = False
		
		if has_nsfw:
			if not get_nsfw and record.get('nsfw'):
				log.append('Filter out on nsfw 1')
				include = False
			if get_nsfw and not record.get('nsfw'):
				log.append('Filter out on nsfw 2')
				include = False
		
		if exact_score == 'no_score':
			if 'score' in record:
				log.append('Filter out on no score')
				include = False
		elif exact_score:
			if exact_score != record.get('score', 0):
				log.append('Filter out on exact score')
				include = False
		elif score and (not record.get('score') or score > record['score']):
			log.append('Filter out on score')
			include = False
		
		if exact_version:
			if exact_version != record['version']:
				log.append('Filter out on exact version')
				include = False
		elif version and version > record['version']:
			log.append('Filter out on version')
			include = False
		
		if image_id != 'latest' and common.convert_to_int(image_id) == record['id'] and log_level >= 6:
			ui.body(f"log: {log}")
		
		if include:
			final_records.append(record)
		
	return final_records
	

def get_image(image_id, body):
	final_records = curate(body, image_id)
	if not final_records:
		return {
			"status": "success",
			"total": 0
		}
	
	final_records = sorted(final_records, key=lambda d: d['create_time'])
	
	if image_id == 'latest':
		if 'mode' in body and body['mode'] == 'shuffle':
			index = random.randrange(len(final_records))
			image_id = final_records[index]['id']
		else:
			image_id = final_records[-1]['id']
	image_id = common.convert_to_int(image_id)
	
	older_id = None
	newer_id = None
	image_record = None
	for record in final_records:
		if image_record:
			newer_id = record['id']
			break
		elif image_id == record['id']:
			image_record = record
		else:
			older_id = record['id']
	
	if not image_record:
		return {
			"status": "fail",
			"total": 0
		}
	
	response = {
		"status": "success",
		"image": image_record,
		"total": len(final_records)
	}
	
	if 'query-artist_id' in image_record:
		artist = collective.get_artist_by_id(image_record['query-artist_id'])
		if artist:
			response['image']['artist'] = artist.data
	
	# Add next and prev records
	if 'mode' in body and body['mode'] == 'shuffle':
		index = random.randrange(len(final_records))
		response['random_id'] = final_records[index]['id']
	else:
		if not newer_id:
			newer_id = final_records[0]['id']
		response['newer_id'] = newer_id
		
		if not older_id:
			older_id = final_records[-1]['id']
		response['older_id'] = older_id
	
	return response


def get_random(body):
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
	
	final_records = sorted(final_records, key=lambda d: d['create_time'])
	
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
	if record['engine_name'] == 'sdxl':
		record['engine_label'] = 'Stable Diffusion XL Beta'
	return record

def get_presigned_url(filename):
	bucket_name = 'artintelligence.gallery'
	object_name = f"images/{filename}"
	bucket = moses_common.s3.Bucket(bucket_name)
	file = moses_common.s3.Object(bucket, object_name)
	return file.get_presigned_url(expiration_time=3600)

def generate(body):
	if 'artist_id' in body:
		artist = collective.get_artist_by_id(body['artist_id'])
		body['artist'] = artist.name
	data = {
		"artist": body.get('artist'),
		"genre": body.get('genre')
	}
	
	function_response = lambda_client.invoke(
		FunctionName = 'art-intelligence-dev-generator',
		InvocationType = 'Event',
		Payload = common.make_json(data)
	)
	
	return {
		"status": "success"
	}

def set_score(body):
	global image_records
	for field in ['filename', 'create_time']:
		if field not in body or not body[field]:
			return error("Missing 'filename' and 'create_time' arguments.")
	record = None
	for image in image_records:
		if image['filename'] == body['filename']:
			record = image
			break
	if not record:
		return error(f"No records matching '{image['filename']}'")
		
	
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
		return error("Failed to save changes to database")
	image_records = read_image_records(images_table)
	return {
		"status": "success",
		"image": record
	}


def get_user_info(access_token):
	url = 'https://auth.artintelligence.gallery/oauth2/userInfo';
	response_code, response_data = common.get_url(url, {
		"bearer_token": access_token,
		"headers": {
			"Content-Type": "application/x-www-form-urlencoded"
		}
	})
	
	if response_code != 200:
		ui.error(f"Failed with code {response_code}")
		return None
	return response_data

def error(message):
	return {
		"status": "error",
		"message": message
	}


