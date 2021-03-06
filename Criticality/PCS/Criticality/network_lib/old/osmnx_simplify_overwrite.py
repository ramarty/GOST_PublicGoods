#overwrite existing OSMNX simplify algorithm

import time
import logging as lg
from shapely.geometry import Point, LineString
from osmnx.utils import log

def is_endpoint(G, node, strict=True):
    """
    Return True if the node is a "real" endpoint of an edge in the network, otherwise False.
    OSM data includes lots of nodes that exist only as points to help streets bend around curves.
    An end point is a node that either:
        1. is its own neighbor, ie, it self-loops
        2. or, has no incoming edges or no outgoing edges, ie, all its incident edges point inward or all its incident edges point outward
        3. or, it does not have exactly two neighbors and degree of 2 or 4
        4. or, if strict mode is false, if its edges have different OSM IDs

    Parameters
    ----------
    G : graph
    node : int, the node to examine
    strict : bool, if False, allow nodes to be end points even if they fail all other rules but have edges with different OSM IDs

    Returns
    -------
    bool
    """
    neighbors = set(list(G.predecessors(node)) + list(G.successors(node)))
    n = len(neighbors)
    d = G.degree(node)

    if G.node[node].get('IsCentroid') == 1:
        return True
    else:

        if node in neighbors:
            # if the node appears in its list of neighbors, it self-loops. this is always an endpoint.
            return True

        # if node has no incoming edges or no outgoing edges, it must be an end point
        elif G.out_degree(node)==0 or G.in_degree(node)==0:
            return True

        elif not (n==2 and (d==2 or d==4)):
            # else, if it does NOT have 2 neighbors AND either 2 or 4 directed edges, it is an endpoint
            # either it has 1 or 3+ neighbors, in which case it is a dead-end or an intersection of multiple streets
            # or it has 2 neighbors but 3 degree (indicating a change from oneway to twoway)
            # or more than 4 degree (indicating a parallel edge) and thus is an endpoint
            return True

        elif not strict:
            # non-strict mode
            osmids = []

            # add all the edge OSM IDs for incoming edges
            for u in G.predecessors(node):
                for key in G.edge[u][node]:
                    osmids.append(G.edge[u][node][key]['osmid'])

            # add all the edge OSM IDs for outgoing edges
            for v in G.successors(node):
                for key in G.edge[node][v]:
                    osmids.append(G.edge[node][v][key]['osmid'])

            # if there is more than 1 OSM ID in the list of edge OSM IDs then it is an endpoint, if not, it isn't
            return len(set(osmids)) > 1

        else:
            # if none of the preceding rules returned true, then it is not an endpoint
            return False


def build_path(G, node, endpoints, path):
    """
    Recursively build a path of nodes until you hit an endpoint node.

    Parameters
    ----------
    G : graph
    node : int, the current node to start from
    endpoints : set, the set of all nodes in the graph that are endpoints
    path : list, the list of nodes in order in the path so far

    Returns
    -------
    paths_to_simplify : list
    """
    # for each successor in the passed-in node
    for successor in G.successors(node):
        if not successor in path:
            # if this successor is already in the path, ignore it, otherwise add it to the path
            path.append(successor)
            if not successor in endpoints:
                # if this successor is not an endpoint, recursively call build_path until you find an endpoint
                path = build_path(G, successor, endpoints, path)
            else:
                # if this successor is an endpoint, we've completed the path, so return it
                return path

    if (not path[-1] in endpoints) and (path[0] in G.successors(path[-1])):
        # if the end of the path is not actually an endpoint and the path's first node is a successor of the
        # path's final node, then this is actually a self loop, so add path's first node to end of path to close it
        path.append(path[0])

    return path


def get_paths_to_simplify(G, strict=True):
    """
    Create a list of all the paths to be simplified between endpoint nodes.
    The path is ordered from the first endpoint, through the interstitial nodes, to the second endpoint.

    Parameters
    ----------
    G : graph
    strict : bool, if False, allow nodes to be end points even if they fail all other rules but have edges with different OSM IDs

    Returns
    -------
    paths_to_simplify : list
    """

    # first identify all the nodes that are endpoints
    start_time = time.time()
    endpoints = set([node for node in G.nodes() if is_endpoint(G, node, strict=strict)])
    log('Identified {:,} edge endpoints in {:,.2f} seconds'.format(len(endpoints), time.time()-start_time))

    start_time = time.time()
    paths_to_simplify = []

    # for each endpoint node, look at each of its successor nodes
    for node in endpoints:
        for successor in G.successors(node):
            if not successor in endpoints:
                # if the successor is not an endpoint, build a path from the endpoint node to the next endpoint node
                try:
                    path = build_path(G, successor, endpoints, path=[node, successor])
                    paths_to_simplify.append(path)
                except RuntimeError:
                    log('Recursion error: exceeded max depth, moving on to next endpoint successor', level=lg.WARNING)
                    # recursion errors occur if some connected component is a self-contained ring in which all nodes are not end points
                    # handle it by just ignoring that component and letting its topology remain intact (this should be a rare occurrence)
                    # RuntimeError is what Python <3.5 will throw, Py3.5+ throws RecursionError but it is a subtype of RuntimeError so it still gets handled

    log('Constructed all paths to simplify in {:,.2f} seconds'.format(time.time()-start_time))
    return paths_to_simplify


def is_simplified(G):
    """
    Determine if a graph has already had its topology simplified. If any of its edges have a
    geometry attribute, we know that it has previously been simplified.

    Parameters
    ----------
    G : graph

    Returns
    -------
    bool
    """
    edges_with_geometry = [d for u, v, k, d in G.edges(data=True, keys=True) if 'geometry' in d]
    return len(edges_with_geometry) > 0


def simplify_graph(G_, strict=True):
    """
    Simplify a graph's topology by removing all nodes that are not intersections or dead-ends.
    Create an edge directly between the end points that encapsulate them,
    but retain the geometry of the original edges, saved as attribute in new edge

    Parameters
    ----------
    G_ : graph
    strict : bool, if False, allow nodes to be end points even if they fail all other rules but have edges with different OSM IDs

    Returns
    -------
    G : graph
    """

    if is_simplified(G_):
        raise Exception('This graph has already been simplified, cannot simplify it again.')

    G = G_.copy()
    initial_node_count = len(list(G.nodes()))
    initial_edge_count = len(list(G.edges()))
    all_nodes_to_remove = []
    all_edges_to_add = []

    # construct a list of all the paths that need to be simplified
    paths = get_paths_to_simplify(G, strict=strict)

    start_time = time.time()
    for path in paths:

        # add the interstitial edges we're removing to a list so we can retain their spatial geometry
        edge_attributes = {}
        for u, v in zip(path[:-1], path[1:]):

            # there shouldn't be multiple edges between interstitial nodes
            edges = G.edge[u][v]
            if not len(edges) == 1:
                log('Multiple edges between "{}" and "{}" found when simplifying'.format(u, v), level=lg.WARNING)

            # the only element in this list as long as above assertion is True (MultiGraphs use keys (the 0 here), indexed with ints from 0 and up)
            edge = edges[0]
            for key in edge:
                if key in edge_attributes:
                    # if this key already exists in the dict, append it to the value list
                    edge_attributes[key].append(edge[key])
                else:
                    # if this key doesn't already exist, set the value to a list containing the one value
                    edge_attributes[key] = [edge[key]]

        for key in edge_attributes:
            # don't touch the length attribute, we'll sum it at the end
            if len(set(edge_attributes[key])) == 1 and not key == 'length':
                # if there's only 1 unique value in this attribute list, consolidate it to the single value (the zero-th)
                edge_attributes[key] = edge_attributes[key][0]
            elif not key == 'length':
                # otherwise, if there are multiple values, keep one of each value
                edge_attributes[key] = list(set(edge_attributes[key]))

        # construct the geometry and sum the lengths of the segments
        edge_attributes['geometry'] = LineString([Point((G.node[node]['x'],
                                                         G.node[node]['y'])) for node in path])
        edge_attributes['length'] = sum(edge_attributes['length'])

        # add the nodes and edges to their lists for processing at the end
        all_nodes_to_remove.extend(path[1:-1])
        all_edges_to_add.append({'origin':path[0],
                                 'destination':path[-1],
                                 'attr_dict':edge_attributes})

    # for each edge to add in the list we assembled, create a new edge between the origin and destination
    for edge in all_edges_to_add:
        G.add_edge(edge['origin'], edge['destination'], **edge['attr_dict'])

    # finally remove all the interstitial nodes between the new edges
    G.remove_nodes_from(set(all_nodes_to_remove))

    msg = 'Simplified graph (from {:,} to {:,} nodes and from {:,} to {:,} edges) in {:,.2f} seconds'
    log(msg.format(initial_node_count, len(list(G.nodes())), initial_edge_count, len(list(G.edges())), time.time()-start_time))
    return G