"""
Parallel differentially private decision tree

@Authors: Sukgamon Sukpisit and Srdjan Skrbic
@Affiliation: Faculty of Sciences, University of Novi Sad, Serbia

This work is supported by the I-BiDaaS project, funded by the European Commission under Grant Agreement No. 780787. 
"""

from sys import float_info

import numpy as np
from numpy.random.mtrand import RandomState
from numpy import linalg as LA
from pycompss.api.api import compss_delete_object
from pycompss.api.parameter import FILE_IN
from pycompss.api.task import task
from dp_test_split import test_split


class DPDecisionTree:
	"""
	Perform Differential privacy decision tree

	Parameters
	----------
	try_features: {None, 'sqrt', 'third', int}
		The number of features to consider when looking for the best split
	max_depth: int
		maximum alowed depth of the tree
	distr_depth: {'auto', int}
		The number of features to consider when looking for the best split
	sklearn_max: float
	privacy_budget: float
		privacy budget, controlling the level of privacy
	bootstrap: boolean
		enable/disable bootstrapping
	random_state: {None, int, instance of RandomState}
		initializing the random state

	"""
	def __init__(self, try_features, max_depth, distr_depth, sklearn_max, privacy_budget, bootstrap, random_state):
		self.try_features = try_features
		self.max_depth = max_depth
		self.distr_depth = distr_depth
		self.sklearn_max = sklearn_max
		self.bootstrap = bootstrap
		self.random_state = random_state

		self.privacy_budget = privacy_budget
		self.epsilon = self.privacy_budget / (2 * (self.max_depth + 1))

		self.n_features = None
		self.n_classes = None

		self.tree = None
		self.nodes_info = None
		self.subtrees = None



	def fit(self, dataset, feature_types):

		count_num_attr = len([a for a in feature_types if a == "num"])

		if count_num_attr > 0:
			self.epsilon = self.privacy_budget / (((2 + count_num_attr) * self.max_depth) + 2)

		self.n_features = dataset.get_n_features()
		self.n_classes = dataset.get_n_classes()
		samples_path = dataset.samples_path
		features_path = dataset.features_path
		n_samples = dataset.get_n_samples()
		y_codes = dataset.get_y_codes()

		seed = self.random_state.randint(np.iinfo(np.int32).max)

		sample, y_s = _sample_selection(n_samples, y_codes, self.bootstrap, seed)

		self.tree = _Node()
		self.nodes_info = []
		self.subtrees = []
		tree_traversal = [(self.tree, sample, y_s, 0)]

		

		while tree_traversal:
			node, sample, y_s, depth = tree_traversal.pop()

			# print("depth=", depth, ", self.distr_depth=", self.distr_depth)

			if depth < self.distr_depth:
				# print("depth < self.distr_depth = True >>>> _split_node_wrapper")

				split = _split_node_wrapper(
					sample, 
					self.n_features, 
					feature_types,
					y_s, 
					self.n_classes, 
					self.try_features, 
					self.random_state, 
					self.epsilon,
					samples_file=samples_path, 
					features_file=features_path)

				node_info, left_group, y_l, right_group, y_r = split
				compss_delete_object(sample)
				compss_delete_object(y_s)
				node.content = len(self.nodes_info)
				self.nodes_info.append(node_info)
				node.left = _Node()
				node.right = _Node()
				depth = depth + 1
				tree_traversal.append((node.right, right_group, y_r, depth))
				tree_traversal.append((node.left, left_group, y_l, depth))
			else:
				# print("depth < self.distr_depth = False >>>> _build_subtree_wrapper")

				subtree = _build_subtree_wrapper(
					sample, 
					y_s, 
					self.n_features,
					feature_types,
					self.max_depth - depth, 
					self.n_classes, 
					self.try_features,
					self.sklearn_max,
					self.random_state,
					samples_path, 
					features_path, 
					self.epsilon)

				node.content = len(self.subtrees)
				self.subtrees.append(subtree)
				compss_delete_object(sample)
				compss_delete_object(y_s)

		self.nodes_info = _merge(*self.nodes_info)


	def predict(self, subset):
		assert self.tree is not None, 'The decision tree is not fitted.'

		branch_predictions = []

		for i, subtree in enumerate(self.subtrees):
			pred = _predict_branch(subset, self.tree, self.nodes_info, i, subtree, self.distr_depth)
			branch_predictions.append(pred)

		return _merge_branches(None, *branch_predictions)


	def predict_proba(self, subset):
		assert self.tree is not None, 'The decision tree is not fitted.'

		branch_predictions = []

		for i, subtree in enumerate(self.subtrees):
			pred = _predict_branch_proba(subset, self.tree, self.nodes_info, i, subtree, self.distr_depth, self.n_classes)
			branch_predictions.append(pred)

		return _merge_branches(self.n_classes, *branch_predictions)


class _Node:
	def __init__(self):
		self.content = None
		self.left = None
		self.right = None


	def predict(self, sample):
		node_content = self.content

		if isinstance(node_content, _LeafInfo):
			return np.full((len(sample),), node_content.mode)

		# if isinstance(node_content, _SkTreeWrapper):
		# 	if len(sample) > 0:
		# 		return node_content.sk_tree.predict(sample)

		if isinstance(node_content, _InnerNodeInfo):
			# pred = np.empty((len(sample),), dtype=np.int64)
			# left_mask = sample[:, node_content.index] <= node_content.value
			# pred[left_mask] = self.left.predict(sample[left_mask])
			# pred[~left_mask] = self.right.predict(sample[~left_mask])

			pred = np.empty((len(sample),), dtype=np.int64)

			if node_content.feature_type == "cat":
				left_mask = sample[:, node_content.index] == node_content.value
			else:
				left_mask = sample[:, node_content.index] <= node_content.value

			pred[left_mask] = self.left.predict(sample[left_mask])
			pred[~left_mask] = self.right.predict(sample[~left_mask])

			return pred

		assert len(sample) == 0, 'Type not supported'
		return np.empty((0,), dtype=np.int64)


	def predict_proba(self, sample, n_classes):
		node_content = self.content

		if isinstance(node_content, _LeafInfo):
			single_pred = node_content.frequencies/node_content.size
			return np.tile(single_pred, (len(sample), 1))

		if isinstance(node_content, _InnerNodeInfo):
			pred = np.empty((len(sample), n_classes), dtype=np.int64)

			if node_content.feature_type == "cat":
				l_msk = sample[:, node_content.index] == node_content.value	
			else:
				l_msk = sample[:, node_content.index] <= node_content.value

			pred[l_msk] = self.left.predict_proba(sample[l_msk], n_classes)
			pred[~l_msk] = self.right.predict_proba(sample[~l_msk], n_classes)

			# l_msk = sample[:, node_content.index] <= node_content.value
			# pred[l_msk] = self.left.predict_proba(sample[l_msk], n_classes)
			# pred[~l_msk] = self.right.predict_proba(sample[~l_msk], n_classes)

			return pred

		assert len(sample) == 0, 'Type not supported'
		return np.empty((0, n_classes), dtype=np.int64)


class _InnerNodeInfo:
	def __init__(self, index=None, value=None, feature_type=None):
		self.index = index
		self.value = value
		self.feature_type = feature_type


class _LeafInfo:
	def __init__(self, size=None, frequencies=None, mode=None):
		self.size = size
		self.frequencies = frequencies
		self.mode = mode


@task(priority=True, returns=2)
def _sample_selection(n_samples, y_codes, bootstrap, seed):

	if bootstrap:
		random_state = RandomState(seed)
		selection = random_state.choice(n_samples, size=n_samples, replace=True)
		selection.sort()

		return selection, y_codes[selection]
	else:
		return np.arange(n_samples), y_codes



def _split_node_wrapper(sample, n_features, feature_types, y_s, n_classes, m_try, random_state, epsilon, samples_file=None, features_file=None):

	seed = random_state.randint(np.iinfo(np.int32).max)

	if features_file is not None:
		return _split_node_using_features(sample, n_features, feature_types, y_s, n_classes, m_try, features_file, seed, epsilon)
	elif samples_file is not None:
		return _split_node(sample, n_features, feature_types, y_s, n_classes, m_try, samples_file, seed, epsilon)
	else:
		raise ValueError('Invalid combination of arguments. samples_file is '
			'None and features_file is None.')



@task(features_file=FILE_IN, returns=(object, list, list, list, list))
def _split_node_using_features(sample, n_features, feature_types, y_s, n_classes, m_try, features_file, seed, epsilon):
	
	features_mmap = np.load(features_file, mmap_mode='r', allow_pickle=False)
	random_state = RandomState(seed)

	return _compute_split(epsilon, sample, n_features, feature_types, y_s, n_classes, m_try, features_mmap, random_state)



@task(samples_file=FILE_IN, returns=(object, list, list, list, list))
def _split_node(sample, n_features, feature_types, y_s, n_classes, m_try, samples_file, seed, epsilon):

	features_mmap = np.load(samples_file, mmap_mode='r', allow_pickle=False).T
	random_state = RandomState(seed)

	return _compute_split(epsilon, sample, n_features, feature_types, y_s, n_classes, m_try, features_mmap, random_state)



def _build_subtree_wrapper(sample, y_s, n_features, feature_types, max_depth, n_classes, m_try, sklearn_max, random_state, samples_file, features_file, epsilon):
	
	seed = random_state.randint(np.iinfo(np.int32).max)

	if features_file is not None:
		return _build_subtree_using_features(
			sample, y_s, n_features, 
			max_depth, n_classes, m_try, 
			sklearn_max, seed, samples_file, 
			features_file, epsilon)

	else:
		return _build_subtree(
			sample, y_s, n_features, 
			feature_types, max_depth, n_classes, 
			m_try, sklearn_max, seed, 
			samples_file, epsilon)



@task(samples_file=FILE_IN, features_file=FILE_IN, returns=_Node)
def _build_subtree_using_features(sample, y_s, n_features, feature_types, max_depth, n_classes, m_try, sklearn_max, seed, samples_file, features_file, epsilon):
	
	random_state = RandomState(seed)

	return _compute_build_subtree(
		epsilon, sample, y_s, 
		n_features, feature_types, max_depth, 
		n_classes, m_try, sklearn_max, 
		random_state, samples_file, use_sklearn=False, features_file=features_file)


@task(samples_file=FILE_IN, returns=_Node)
def _build_subtree(sample, y_s, n_features, feature_types, max_depth, n_classes, m_try, sklearn_max, seed, samples_file, epsilon):
	
	random_state = RandomState(seed)

	return _compute_build_subtree(
		epsilon, sample, y_s, 
		n_features, feature_types, max_depth, 
		n_classes, m_try, sklearn_max, 
		random_state, samples_file, use_sklearn=False)



def _compute_build_subtree(epsilon, sample, y_s, n_features, feature_types, max_depth, n_classes, m_try, sklearn_max, random_state, samples_file, features_file=None, use_sklearn=True):
	
	# print("sample=", sample)

	if not sample.size:
		return _Node()

	if features_file is not None:
		mmap = np.load(features_file, mmap_mode='r', allow_pickle=False)
	else:
		mmap = np.load(samples_file, mmap_mode='r', allow_pickle=False).T

	subtree = _Node()
	tree_traversal = [(subtree, sample, y_s, 0)]

	while tree_traversal:

		node, sample, y_s, depth = tree_traversal.pop()

		if depth < max_depth:
			split = _compute_split(epsilon, sample, n_features, feature_types, y_s, n_classes, m_try, mmap, random_state)
			node_info, left_group, y_l, right_group, y_r = split
			node.content = node_info

			if isinstance(node_info, _InnerNodeInfo):
				node.left = _Node()
				node.right = _Node()
				tree_traversal.append((node.right, right_group, y_r, depth + 1))
				tree_traversal.append((node.left, left_group, y_l, depth + 1))
		else:
			node.content = _compute_leaf_info(y_s, n_classes, epsilon)

	return subtree



def _compute_split(epsilon, sample, n_features, feature_types, y_s, n_classes, m_try, features_mmap, random_state):

	node_info = left_group = y_l = right_group = y_r = None
	split_ended = False
	tried_indices = []

	while not split_ended:

		untried_indices = np.setdiff1d(np.arange(n_features), tried_indices)
		index_selection = _feature_selection(untried_indices, m_try, random_state)

		b_score = float_info.max
		b_index = None
		b_value = None

		values = []
		scores = []

		for index in index_selection:

			feature = features_mmap[index]
			score, value = test_split(sample, y_s, feature, feature_types[index], n_classes, epsilon)

			f_idx = index
			f_value = value

			scores.append(score)
			values.append(value)

		if len(scores) > 1:
			const = epsilon / (2 * 2)
			q_scores = np.array(scores)
			q = q_scores / LA.norm(const * q_scores, 1)
			weights = np.exp(q)
			prob = weights / LA.norm(weights, 1)
			max_choice = np.random.choice(np.array(scores), p=prob) #exponential mechanism
			att = scores.index(max_choice)
			val = values[att]

			b_index = index_selection[att]
			b_value = val
		else:
			b_index = f_idx
			b_value = f_value

		# print("scores=", scores)
		# print("b_value=", b_value)
		# print("b_index=", b_index)

		groups = _get_groups(sample, y_s, features_mmap, b_index, b_value, feature_types[b_index])
		left_group, y_l, right_group, y_r = groups

		if left_group.size and right_group.size:
			split_ended = True
			node_info = _InnerNodeInfo(b_index, b_value, feature_types[b_index])
		else:
			tried_indices.extend(list(index_selection))
			if len(tried_indices) == n_features:
				split_ended = True
				node_info = _compute_leaf_info(y_s, n_classes, epsilon)
				left_group = sample
				y_l = y_s
				right_group = np.array([], dtype=np.int64)
				y_r = np.array([], dtype=np.int8)


	return node_info, left_group, y_l, right_group, y_r



def _feature_selection(untried_indices, m_try, random_state):
	selection_len = min(m_try, len(untried_indices))
	return random_state.choice(untried_indices, size=selection_len, replace=False)



def _get_groups(sample, y_s, features_mmap, index, value, feature_type):

	# print("index=", index)
	# print("value=", value)
	# print("feature_type=", feature_type)

	if index is None:
		empty_sample = np.array([], dtype=np.int64)
		empty_labels = np.array([], dtype=np.int8)

		return sample, y_s, empty_sample, empty_labels

	feature = features_mmap[index][sample]

	if (feature_type == "cat"):
		mask = (feature == value)
	else:
		mask = feature < value

	left = sample[mask]
	right = sample[~mask]
	y_l = y_s[mask]
	y_r = y_s[~mask]

	
	# mask = feature < value

	# left = sample[mask]
	# right = sample[~mask]
	# y_l = y_s[mask]
	# y_r = y_s[~mask]


	return left, y_l, right, y_r



def _compute_leaf_info(y_s, n_classes, epsilon):

	# print(">>>>_compute_leaf_info")

	# print("y_s=", y_s)
	# print("epsilon=", epsilon)

	# frequencies = np.bincount(y_s, minlength=n_classes)
	# print("frequencies = np.bincount(y_s, minlength=n_classes)=", frequencies)
	# mode = np.argmax(frequencies)
	# print("mode = np.argmax(frequencies)=", mode)

	frequencies = np.bincount(y_s, minlength=n_classes)
	for i in range(len(frequencies)):
		noise = int(np.random.laplace(frequencies[i], 1 / epsilon))
		frequencies[i] = abs(noise)

	mode = np.argmax(frequencies)

	return _LeafInfo(len(y_s), frequencies, mode)


def _compute_split_for_num_attribute(sample, feature, value):

	# f = feature[sample]

	# ''' Sort from fmin->fmax'''
	# sort_indices = np.argsort(f)

	# f_sorted = f[sort_indices]

	# mask = f_sorted < value
	# l_range = f_sorted[mask]
	# r_range = f_sorted[~mask]

	# print("l_range=", l_range)
	# print("r_range=", r_range)

	return value


@task(returns=list)
def _merge(*object_list):
	return object_list



@task(returns=1)
def _predict_branch(subset, tree, nodes_info, subtree_index, subtree, distr_depth):
	samples = subset.samples
	path = _get_subtree_path(subtree_index, distr_depth)
	indices_mask = _get_predicted_indices(samples, tree, nodes_info, path)
	prediction = subtree.predict(samples[indices_mask])

	return indices_mask, prediction



@task(returns=1)
def _predict_branch_proba(subset, tree, nodes_info, subtree_index, subtree, distr_depth, n_classes):
	samples = subset.samples
	path = _get_subtree_path(subtree_index, distr_depth)
	indices_mask = _get_predicted_indices(samples, tree, nodes_info, path)
	prediction = subtree.predict_proba(samples[indices_mask], n_classes)

	return indices_mask, prediction

@task(returns=list)
def _merge_branches(n_classes, *predictions):
	samples_len = len(predictions[0][0])

	if n_classes is not None:
		shape = (samples_len, n_classes)
	else:
		shape = (samples_len,)

	merged_prediction = np.empty(shape, dtype=np.int64)

	for selected, prediction in predictions:
		merged_prediction[selected] = prediction

	return merged_prediction



def _get_subtree_path(subtree_index, distr_depth):
	if distr_depth == 0:
		return ''

	return bin(subtree_index)[2:].zfill(distr_depth)


def _get_predicted_indices(samples, tree, nodes_info, path):
	idx_mask = np.full((len(samples),), True)

	for direction in path:
		node_info = nodes_info[tree.content]
		if isinstance(node_info, _LeafInfo):
			if direction == '1':
				idx_mask[:] = 0
		else:
			col = node_info.index
			value = node_info.value

			if direction == '0':
				idx_mask[idx_mask] = samples[idx_mask, col] <= value
				tree = tree.left
			else:
				idx_mask[idx_mask] = samples[idx_mask, col] > value
				tree = tree.right
	return idx_mask
