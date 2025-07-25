"""

..    airfoil
      -------

    Contains a class for creating, modifying and exporting airfoils.


"""

import warnings
import numpy as np
from pyspline import Curve
from scipy.optimize import brentq, newton, minimize
from . import sampling
from .utils.geom_ops import _translateCoords, _rotateCoords, _scaleCoords, _getClosestY
from .utils.io_utils import _writeDat, _writePlot3D, _writeFFD, Error


EPS = np.finfo(np.float64).eps
ZEROS_2 = np.zeros(2)


class Airfoil:
    """
    A class for manipulating airfoil geometry.

    Create an instance of an airfoil by passing in a set of points.
    The points must satisfy the following requirements:

        - Ordered such that they form a continuous airfoil surface
        - First and last points correspond to trailing edge

            - If the two points coincide, the airfoil will be considered sharp/round
            - Otherwise, a blunt (open) TE will be assumed

    It is not necessary for the points to be in a counter-clockwise ordering. If
    they are not ordered counter-clockwise, the order will be reversed so that
    all functions can be written to expect the spline to begin at the upper
    surface of the trailing edge and end at the lower surface of the trailing
    edge.

    See documentation in :class:`pySpline <pyspline:pyspline.pyCurve.Curve>` for information on the spline representation

    Parameters
    ----------
    coords : ndarray [N,2]
        The coordinate pairs defining the airfoil

    spline_order : {4, 2, 3}
        Order of the spline. :math:`n` order implies :math:`C^{n-2}` continuity

    normalize : bool
        True to normalize the chord of the airfoil, set to zero angle of attack, and move the leading edge to the origin

    nCtl : int
        If this is set to an integer the underlying airfoil spline will be created using least mean squares (LMS) instead of interpolation. The number of control points used in the LMS will be equal to nCtl.

    """

    def __init__(self, coords, spline_order=4, normalize=False, nCtl=None):
        self.spline_order = spline_order
        self.sampled_pts = None
        self.closedCurve = None
        self.camber = None
        self.british_thickness = None
        self.american_thickness = None
        self.nCtl = nCtl

        # Initialize geometric information
        self.recompute(coords)

        if normalize:
            self.normalizeAirfoil()

    def recompute(self, coords):
        """
        Recomputes the underlying spline and relevant parameters from the given set of coordinates.

        Parameters
        ----------
        coords : Ndarray [N,2]
            The coordinate pairs to compute the airfoil spline from

        """
        if self.nCtl:
            self.spline = Curve(X=coords, k=self.spline_order, nCtl=self.nCtl)
        else:
            self.spline = Curve(X=coords, k=self.spline_order)

        self.reorder()

        self.TE = self.getTE()
        self.LE, self.s_LE = self.getLE()
        self.chord = self.getChord()
        self.twist = self.getTwist()
        self.closedCurve = (coords[0, :] == coords[-1, :]).all()
        self.sampled_pts = None

        camber_pts = self.getCDistribution(coords.size)
        self.camber = Curve(X=camber_pts, k=3)
        self.british_thickness = Curve(X=self.getThickness(coords.size, "british"), k=3)
        self.american_thickness = Curve(X=self.getThickness(coords.size, "american"), k=3)

    def reorder(self):
        """
        This function orients the points counter-clockwise
        """

        coords = self.spline.X
        N = coords.shape[0]

        # Accumulate the signed area under each line segment connecting adjacent points
        # If the total area is positive, the points are oriented clockwise
        area = 0
        for i in range(N):
            area += (coords[i, 0] - coords[i - 1, 0]) * (coords[i, 1] + coords[i - 1, 1])

        if area > 0:
            # Flip orientation to counter-clockwise
            self.recompute(self.spline.X[::-1, :])

    ## Geometry Information

    def getCamber(self):
        """
        Calculates the camber spline defined by the airfoil

        Returns
        -------
        camber : pySpline curve object
            The spline that defines the camberline from s = 0 at the leading edge to s = 1 at the trailing edge.
        """

        return self.camber

    def getTE(self):
        """
        Calculates the trailing edge point on the spline

        Returns
        -------
        TE : ndarray [2]
            The coordinate of the trailing edge of the airfoil
        """

        TE = (self.spline.getValue(0) + self.spline.getValue(1)) / 2
        return TE

    def getLE(self):
        r"""
        Calculates the leading edge point on the spline, which is defined as the point furthest away from the TE. The spline is assumed to start at the TE. The routine uses a root-finding algorithm to compute the LE.

        Returns
        -------
        LE : Ndarray [2]
            the coordinate of the leading edge

        s_LE : float
            the parametric position of the leading edge

        Notes
        -----
        Let the TE be at point :math:`x_0, y_0`, then the Euclidean distance between the TE and any point on the airfoil spline is :math:`\ell(s) = \sqrt{\Delta x^2 + \Delta y^2}`, where :math:`\Delta x = x(s)-x_0` and :math:`\Delta y = y(s)-y_0`. We know near the LE, this quantity is concave. Therefore, to find its maximum, we differentiate and use a root-finding algorithm on its derivative.
        :math:`\\frac{\mathrm{d}\ell}{\mathrm{d}s} = \\frac{\Delta x\\frac{\mathrm{d}x}{\mathrm{d}s} + \Delta y\\frac{\mathrm{d}y}{\mathrm{d}s}}{\ell}`

        The function ``dellds`` computes the quantity :math:`\Delta x\\frac{\mathrm{d}x}{\mathrm{d}s} + \Delta y\\frac{\mathrm{d}y}{\mathrm{d}s}` which is then used by ``brentq`` to find its root, with an initial bracket at :math:`[0.3, 0.7]`.

        """

        def dellds(s, spline, TE):
            pt = spline.getValue(s)
            deriv = spline.getDerivative(s)
            dx = pt[0] - TE[0]
            dy = pt[1] - TE[1]
            return dx * deriv[0] + dy * deriv[1]

        s_LE = brentq(dellds, 0.3, 0.7, args=(self.spline, self.TE))
        LE = self.spline.getValue(s_LE)

        return LE, s_LE

    def getTwist(self):
        """
        Calculates the twist of the airfoil using the leading and trailing edge points

        Returns
        -------
        twist : float
            The twist in degrees of the airfoil

        """

        chord_vec = self.TE - self.LE
        twist = np.arctan2(chord_vec[1], chord_vec[0]) * 180 / np.pi
        # twist = np.arccos(normalized_chord.dot(np.array([1., 0.]))) * np.sign(normalized_chord[1])
        return twist

    def getChord(self):
        """
        Calculates the chord of the airfoil as the distance between the leading and trailing edges

        Returns
        -------
        chord : float
            The chord length
        """

        chord = np.linalg.norm(self.TE - self.LE)
        return chord

    def getSplinePts(self):
        """
        alias for returning the points that make the airfoil spline

        Returns
        -------
        X : Ndarry [N, 2]
            the coordinates that define the airfoil spline
        """
        return self.spline.X

    def findPt(self, position, axis=0, s_0=0):
        """
        finds that point at the intersection of the plane defined by the axis and the postion
        and the airfoil curve

        Parameters
        ----------
        position : float
            the position of the plane on the given axis

        axis : int
            the axis the plane will intersect 0 for x and 1 for y

        s_0 : float
            an initial guess for the parametric position of the solution

        Returns
        -------
        X : Ndarray [2]
            The coordinate at the intersection

        s_x : float
            the parametric location of the intersection
        """

        def err(s):
            return self.spline(s)[axis] - position

        def err_deriv(s):
            return self.spline.getDerivative(s)[axis]

        s_x = newton(err, s_0, fprime=err_deriv)

        return self.spline.getValue(s_x), s_x

    def getTEThickness(self):
        """
        gets the trailing edge thickness for the airfoil

        Returns
        -------
        TE_thickness : float
            the trailing edge thickness
        """
        top = self.spline.getValue(0)
        bottom = self.spline.getValue(1)
        TE_thickness = np.sqrt((top[0] - bottom[0]) ** 2 + (top[1] - bottom[1]) ** 2)
        return TE_thickness

    def getLERadius(self):
        """
        Computes the leading edge radius of the airfoil. Note that this is heavily dependent on the initialization points, as well as the spline order/smoothing.

        Returns
        -------
        LE_rad : float
            The leading edge radius
        """
        # if self.s_LE is None:
        #     self.getLE()

        first = self.spline.getDerivative(self.s_LE)
        second = self.spline.getSecondDerivative(self.s_LE)
        LE_rad = np.linalg.norm(first) ** 3 / np.linalg.norm(first[0] * second[1] - first[1] * second[0])
        return LE_rad

    def getCDistribution(self, nPts):
        """
        Return the coordinates of the camber points

        Parameters
        ----------
        nPts : int
            The number of points to sample

        Returns
        -------
        camber_pts : Ndarray [nPts, 2]
            the locations of the camber points of the airfoil starting with the leading edge and ending with the trailing edge

        """
        top_surf, bottom_surf = self.splitAirfoil()

        # Compute the chord
        chord_pts = np.vstack([self.LE, self.TE])
        chord = Curve(X=chord_pts, k=2)

        # Sampling along airfoil for camber points
        lin_sampling = np.linspace(0, 1, nPts - 1, endpoint=False)[1:]

        chord_pts = chord.getValue(lin_sampling)
        camber_pts = np.zeros((nPts - 2, 2))

        # At each point we are looking for the camber
        for j in range(chord_pts.shape[0]):
            # Get the direction normal to the chord line
            direction = np.array(
                [np.cos(np.pi / 2 + np.deg2rad(self.twist)), np.sin(np.pi / 2 + np.deg2rad(self.twist))]
            )
            direction = direction / np.linalg.norm(direction)

            # Draw a ray through the airfoil in the given direction
            top = chord_pts[j, :] + 1 * self.chord * direction
            bottom = chord_pts[j, :] - 1 * self.chord * direction
            temp = np.vstack((top, bottom))
            normal = Curve(X=temp, k=2)

            # Determine the intersection of this ray with both the upper and lower surfaces
            s_top, _, _ = top_surf.projectCurve(normal, nIter=5000, eps=EPS)
            s_bottom, _, _ = bottom_surf.projectCurve(normal, nIter=5000, eps=EPS)
            intersect_top = top_surf.getValue(s_top)
            intersect_bottom = bottom_surf.getValue(s_bottom)

            # Compute the camber
            camber_pts[j, :] = (intersect_top + intersect_bottom) / 2

        # Add TE and LE to the camber points.
        camber_pts = np.vstack((self.LE, camber_pts, self.TE))
        return camber_pts

    def getThickness(self, nPts, tType):
        """
        Computes the thicknesses at each x stations spaced linearly along the airfoil

        Parameters
        ----------
        nPts : int
            number of points to sample including the edge

        tType : str
            either "american" or "british"

        Returns
        -------
        thickness_pts : Ndarray [nPts, 2]
            The thickness at each x station
        """

        if tType not in ["american", "british"]:
            raise Error("Do not recognize thickness type!")

        top_surf, bottom_surf = self.splitAirfoil()

        # The parametric spline values along the camber line to find thickness points
        s = np.linspace(0, 1, nPts - 1, endpoint=False)[1:]
        thickness_pts = np.zeros((nPts - 2, 2))

        # Find thickness at each point
        for j in range(len(s)):
            # If british we project a ray normal to chordline
            if tType == "british":
                direction = np.array(
                    [np.cos(np.pi / 2 - np.deg2rad(self.twist)), np.sin(np.pi / 2 - np.deg2rad(self.twist))]
                )
            # If american we project a ray normal to camberline
            else:
                dx = self.camber.getDerivative(s[j])
                direction = np.array([-dx[1], dx[0]])
            direction = direction / np.linalg.norm(direction)

            # create a ray through the upper and lower surfaces from given direction
            top = self.camber.getValue(s[j]) + 10 * self.chord * direction
            bottom = self.camber.getValue(s[j]) - 10 * self.chord * direction
            normal = Curve(X=np.vstack([top, bottom]), k=2)

            # approximate location of the intersection as a percentage of the chord
            s_guess = (normal.getValue(0.5)[0] - self.LE[0]) / self.chord

            # top surf goes from 0 at TE to 1 at LE, so parameter needs to be reversed
            top_guess = 1 - s_guess
            bottom_guess = s_guess

            # Keep guesses within the bounds of the spline
            if s_guess > 1:
                top_guess = 0
                bottom_guess = 1
            elif s_guess < 0:
                top_guess = 1
                bottom_guess = 0

            # Find upper and lower intersections
            s_top, _, _ = top_surf.projectCurve(normal, nIter=100, eps=EPS, s=top_guess, t=0.5)
            s_bottom, _, _ = bottom_surf.projectCurve(normal, nIter=100, eps=EPS, s=bottom_guess, t=0.5)

            # Compute the thickness
            thickness_pts[j, 0] = self.camber.getValue(s[j])[0]
            if tType == "british":
                thickness_pts[j, 1] = top_surf.getValue(s_top)[1] - bottom_surf.getValue(s_bottom)[1]
            else:
                x_top = top_surf.getValue(s_top)
                x_bottom = bottom_surf.getValue(s_bottom)
                thickness_pts[j, 1] = np.linalg.norm(x_top - x_bottom)

        # Add the trailing and leading edge points when we return
        return np.vstack([[self.LE[0], 0], thickness_pts, [self.TE[0], self.getTEThickness()]])

    def getTEAngle(self):
        """
        Computes the trailing edge angle of the airfoil. We assume here that the spline goes from top to bottom, and that s=0 and s=1 corresponds to the
        top and bottom trailing edge points. Whether or not the airfoil is closed is irrelevant.

        Returns
        -------
        TE_angle : float
            The angle of the trailing edge in degrees
        """
        top = self.spline.getDerivative(0)
        top = top / np.linalg.norm(top)
        bottom = self.spline.getDerivative(1)
        bottom = bottom / np.linalg.norm(bottom)
        # print(np.dot(top,bottom))
        TE_angle = np.pi - np.arccos(np.dot(top, bottom))
        return np.rad2deg(TE_angle)

    def getMaxThickness(self, tType):
        """
        This function returns the maximum relative thickness value.

        Parameters
        ----------
        tType : str
            Can be one of 'british' or 'american'

        Returns
        -------
        x_loc : float
            The x station containing the maximum thickness

        max_thickness : float
            the maximum thickness of the airfoil

        """
        if tType not in ["american", "british"]:
            raise Error("Do not recognize thickness type!")

        def american_f(s):
            return -self.american_thickness.getValue(s)[1]

        def american_df(s):
            return -self.american_thickness.getDerivative(s)[1]

        def british_f(s):
            return -self.british_thickness.getValue(s)[1]

        def british_df(s):
            return -self.british_thickness.getDerivative(s)[1]

        if tType == "american":
            opt = minimize(american_f, 0.5, method="SLSQP", jac=american_df, bounds=[(0, 1)])

            if not opt.success:
                raise Error("Could not determine the maximum thickness.")

            opt_point = self.american_thickness.getValue(opt.x)

        else:
            opt = minimize(british_f, 0.5, method="SLSQP", jac=british_df, bounds=[(0, 1)])

            if not opt.success:
                raise Error("Could not determine the maximum thickness.")

            opt_point = self.british_thickness.getValue(opt.x)

        return opt_point[0], opt_point[1]

    def _findChordProj(self, coord):
        """
        Finds the point on the chordline that defines a line from `coord` to the chordline that is perpendicular to the chordline

        Parameters
        ----------
        coord : ndarray [2]
            The point of interest we wish to project onto the chordline.

        Returns
        -------
        point : ndarray [2]
            The coordinate that is the perpendicular projection of `coord` onto the chordline
        """
        # vector defines the chord
        chord = self.LE - self.TE

        # Parametric position of point on chordline
        s = (-chord[0] * (self.TE[0] - coord[0]) - chord[1] * (self.TE[1] - coord[1])) / (chord[0] ** 2 + chord[1] ** 2)

        return self.TE + s * chord

    def _MaxCamberOptimize(self, maximum):
        """
        Used to compute the most negative and most positive cambers of an airfoil

        Parameters
        ----------
        maximum : bool
            If true find most positive, if false find most negative


        Returns
        -------
        x_loc : float
            the x location of the maximum camber

        max_camber : float
            the maximum camber
        """

        def f(s, factor):
            # Find the perpindicular project onto the chord line
            pointInterest = self.camber.getValue(s)
            chordProj = self._findChordProj(pointInterest)

            # Determine if distance is +/- with cross product
            chord = self.LE - self.TE
            chord /= np.linalg.norm(chord)
            direction = pointInterest - chordProj
            direction /= np.linalg.norm(direction)
            cross = direction[0] * chord[1] - direction[1] * chord[0]

            return cross * factor * np.linalg.norm(pointInterest - chordProj)

        if maximum:
            factor = -1
        else:
            factor = 1

        opt = minimize(lambda s: f(s, factor), 0.5, method="SLSQP", bounds=[(0, 1)])

        if not opt.success:
            if maximum:
                raise Error("Could not determine maximum camber.")
            raise Error("Could not determine minimum camber.")

        opt_point = self.camber.getValue(opt.x)

        opt_int = self._findChordProj(opt_point)

        # convert to airfoil coordinates
        x = np.linalg.norm(opt_int - self.LE) / np.linalg.norm(self.LE - self.TE)
        c = factor * f(opt.x, factor) / np.linalg.norm(self.LE - self.TE)

        return x, c

    def getMaxCamber(self):
        """
        This function returns the maximum camber value.

        Returns
        -------
        x_loc : float
            the x location of the maximum camber

        max_camber : float
            the maximum camber of the airfoil

        """

        return self._MaxCamberOptimize(True)

    def getMinCamber(self):
        """
        This function returns the minimum camber value.

        Returns
        -------
        x_loc : flaot
            the x location of the maximum ngative camber
        min_camber : float
            the maximum negative camber of the airfoil
        """

        return self._MaxCamberOptimize(False)

    def isReflex(self):
        """
        Determines if an airfoil is reflex by checking if the derivative of the camber line at the trailing edge is positive.

        Returns
        -------
        reflexive : bool
            True if reflexive
        """
        if self.camber is None:
            self.getCamber()

        return self.camber.getDerivative(1)[1] > 0

    def isSymmetric(self, tol=1e-6):
        """
        Checks if an airfoil is symmetric

        Parameters
        ----------
        tol : float
            tolerance for camber line to still be consdiered symmetrical

        Returns
        -------
        symmetric : bool
            True if the airfoil is symmetric within the given tolerance
        """

        if abs(self.getMinCamber()[1]) < tol and abs(self.getMaxCamber()[1]) < tol:
            return True

        return False

    # ==============================================================================
    # Geometry Modification
    # ==============================================================================

    def rotate(self, angle, origin=ZEROS_2):
        """
        rotates the airfoil about the specified origin

        Parameters
        ----------
        angle : float
            the angle to rotate the airfoil in degrees

        origin : Ndarray [2]
            the point about which to rotate the airfoil
        """
        new_coords = _rotateCoords(self.spline.X, np.deg2rad(angle), origin)

        self.recompute(new_coords)

    def derotate(self, origin=ZEROS_2):
        """
        derotates the airfoil about the origin by the twist

        Parameters
        ----------
        origin : Ndarray [2]
            the location about which to preform the rotation
        """
        self.rotate(-1.0 * self.twist, origin=origin)

    def scale(self, factor, origin=ZEROS_2):
        """
        Scale the airfoil by factor about the origin

        Parameters
        ----------
        factor : float
            the scaling factor

        origin : Ndarray [2]
            the coordinate about which to preform the scaling
        """

        new_coords = _scaleCoords(self.spline.X, factor, origin)
        self.recompute(new_coords)

    def normalizeChord(self, origin=ZEROS_2):
        """
        Set the chord to 1 by scaling the airfoil about the given origin

        Parameters
        ----------
        origin : Ndarray [2]
            the point about which to scale the airfoil
        """

        if self.chord != 1:
            self.scale(1.0 / self.chord, origin=origin)

    def translate(self, delta):
        """
        Translate the airfoil by the vector delta

        Parameters
        ----------
        delta : Ndarray [2]
            the vector that defines the translation of the airfoil
        """

        coords = _translateCoords(self.spline.X, delta)
        self.recompute(coords)

    def center(self):
        """
        Move the airfoil so that the leading edge is at the origin
        """

        if not np.all(self.LE == ZEROS_2):
            self.translate(-1.0 * self.LE)

    def splitAirfoil(self):
        """
        Splits the airfoil into upper and lower surfaces

        Returns
        -------
        top : pySpline curve object
            A spline that defines the upper surface

        bottom : pySpline curve object
            A spline that defines the lower surface
        """

        # if self.s_LE is None:
        # self.getLE()
        top, bottom = self.spline.splitCurve(self.s_LE)
        return top, bottom

    def normalizeAirfoil(self, derotate=True, normalize=True, center=True):
        """
        Sets the twist to zero, the chord to one, and the leading edge location to the origin

        Parameters
        ----------
        derotate : bool
            True to set twist to zero

        normalize : bool
            True to set the chord length to one

        center : bool
            True to put the leading edge at the origin
        """

        if derotate or normalize or center:
            # Order of operation here is important, even though all three operations are linear, because
            # we rotate about the origin for simplicity.
            if center:
                self.center()
            if derotate:
                self.derotate()
            if normalize:
                self.normalizeChord()

    def makeBluntTE(self, xCut=0.98, top_guess=0.5, bottom_guess=0.5):
        """
        This cuts the upper and lower surfaces to creates a blunt trailing edge perpendicular to the chord line.

        Parameters
        ----------
        xCut : float, optional
            the location to cut the blunt TE **as a percentage of the chord**

        top_guess : float, optional
            The parametric location guess for the top surface intersection

        bottom_guess : float, optional
            The parametric location guess for the bottom surface intersection

        """
        # Find global coordinates of cut point
        xCut_global = self.LE + xCut * (self.TE - self.LE)

        # The direction normal to the chordline
        direction = np.array([np.cos(np.pi / 2 + np.deg2rad(self.twist)), np.sin(np.pi / 2 + np.deg2rad(self.twist))])
        direction = direction / np.linalg.norm(direction)

        # ray to intersect upper and lower surfaces
        ray = [xCut_global - 2 * direction * self.getChord(), xCut_global + 2 * direction * self.getChord()]
        top_surf, bottom_surf = self.splitAirfoil()
        normal = Curve(X=ray, k=2)

        # Get intersections
        s_top, _, _ = top_surf.projectCurve(normal, nIter=5000, eps=EPS, s=top_guess, t=0.5)
        s_bottom, _, _ = bottom_surf.projectCurve(normal, nIter=5000, eps=EPS, s=bottom_guess, t=0.5)

        if s_top == 0.0:
            warnings.warn(
                "makeBluntTE did not cut the top surface. Try again with a different top_guess.", stacklevel=2
            )
        if s_bottom == 1.0:
            warnings.warn(
                "makeBluntTE did not cut the bottom surface. Try again with a different bottom_guess.", stacklevel=2
            )

        # Get all the coordinates that will not be cut off
        coords = [top_surf.getValue(s_top)]
        chord = self.LE - self.TE
        for x in self.getSplinePts():
            # dot product test checks for positive projection onto chord
            current_direction = x - xCut_global
            if chord[0] * current_direction[0] + chord[1] * current_direction[1] > 0:
                coords.append(np.array(x))

        coords.append(np.array(bottom_surf.getValue(s_bottom)))
        self.recompute(np.array(coords))

    def sharpenTE(self, xCut=0.98):
        """
        this method creates a sharp trailing edge **from a blunt one** by projecting straight lines from the upper and lower surfacs of a blunt trailing edge.

        Parameters
        ----------
        xCut : float
            x location **as a percentage of chord** to cut off the current trailing edge if it is not already blunt.
        """
        if xCut >= 1.0 or xCut <= 0:
            raise Error("xCut must be between 0 and 1.")

        if self.closedCurve:
            self.makeBluntTE(xCut)

        # Value of blunt TE point on upper surface
        val_u = self.spline.getValue(0)
        # derivative of blunt TE point of upper surface wrt parametric parameter
        ds_u = self.spline.getDerivative(0)
        # slope of blunt TE point of upper surface
        dx_u = ds_u[1] / ds_u[0]

        # Value of blunt TE point on lower surface
        val_l = self.spline.getValue(1)
        # derivative of blunt TE point of lower surface wrt parametric parameter
        ds_l = self.spline.getDerivative(1)
        # slope of blunt TE point of lower surface
        dx_l = ds_l[1] / ds_l[0]

        # make sure that the slope of the lower surface is greater than the upper, ensuring the points will intersect
        if dx_u == dx_l:
            raise Error("Slopes at blunt TE are parallel, no intersection point for a sharp TE.")
        if dx_u > dx_l:
            raise Error("Slopes at blunt TE indicate an intersection towards the LE of the airfoil.")

        # calculate the x location of the intersection
        x = (val_l[1] - val_u[1] - val_l[0] * dx_l + val_u[0] * dx_u) / (dx_u - dx_l)
        # calculate the y location of the intersection
        y = val_l[1] + dx_l * (x - val_l[0])

        # add intersection points and then recompute the airfoil
        coords = np.vstack(([x, y], self.spline.X, [x, y]))
        self.recompute(coords)

    def roundTE(self, xCut=0.98, k=4, nPts=20, dist=0.4):
        """
        this method creates a smooth round trailing edge **from a blunt one** using a spline. If the trailing edge is not already blunt xCut specifies the location of the cut

        Parameters
        ----------
        xCut : float
            x location of the cut **as a percentage of the chord**. Will not do anything if the TE is already blunt.
        k: int (3 or 4)
            order of the spline used to make the rounded trailing edge of the airfoil.
        nPts : int
            Number of trailing edge points to add to the airfoil spline
        dist : float
            Arbitrary factor that specifies how long to make the added TE. Larger dist corresponds to longer addition to the end

        """
        if xCut >= 1.0 or xCut <= 0:
            raise Error("xCut must be between 0 and 1.")

        if self.closedCurve:
            self.makeBluntTE(xCut)

        # unit length for making rounded TE
        dx = self.getTEThickness() * dist

        # create the knot vector for the spline
        t = [0] * k + [0.5] + [1] * k

        # create the vector of control points for the spline
        coeff = np.zeros((k + 1, 2))

        for ii in [0, -1]:
            coeff[ii] = self.spline.getValue(np.abs(ii))
            dX_ds = self.spline.getDerivative(np.abs(ii))
            dy_dx = dX_ds[1] / dX_ds[0]

            # the indexing here is a bit confusing.ii = 0 -> coeff[1] and ii = -1 -> coef[-2]
            coeff[3 * ii + 1] = np.array([coeff[ii, 0] + dx * 0.5, coeff[ii, 1] + dy_dx * dx * 0.5])

        if k == 4:
            chord = self.TE - self.LE
            chord /= np.linalg.norm(chord)
            coeff[2] = np.array([self.TE[0] + chord[0] * dx, self.TE[1] + chord[1] * dx])

        ## make the TE curve
        te_curve = Curve(t=t, k=k, coef=coeff)

        # ----- combine the TE curve with the spline curve -----
        upper_curve, lower_curve = te_curve.splitCurve(0.5)
        upper_pts = upper_curve.getValue(np.linspace(1, 0, nPts // 2))
        lower_pts = lower_curve.getValue(np.linspace(1, 0, nPts // 2))

        coords = np.vstack((upper_pts[:-1], self.spline.X, lower_pts[1:]))

        # ---- recompute with new TE ---
        self.recompute(coords)

    def removeTE(self, tol=0.3, xtol=0.9):
        """
        Removes points from the trailing edge of an airfoil, and recomputes the underlying spline.

        Parameters
        ----------
        tol : float
            A point is part of the trailing edge if the magnitude of the dot product of the normalized vector of `coord[i+1]-coord[i]` and the normalized vector from the trailing edge to the leading edge is less than this tolerance.
            This means that decreasing the tolerance will require the orientation of an element to approach being perpendicular to the chord to be consider part of the trailing edge.

        xtol : float
            Only checks for trailing edge points if the coodinate is past this fraction of the chord.

        Returns
        -------
        TE_points : Ndarray [N,2]
            The points that were flagged as trailing edge points and removed from the airfoil coordinates.
        """
        coords = self.getSplinePts()
        chord_vec = self.TE - self.LE
        unit_chord_vec = chord_vec / np.linalg.norm(chord_vec)

        airfoil_mask = []
        TE_mask = []
        for ii in range(coords.shape[0] - 1):  # loop over each element
            if coords[ii, 0] >= (self.LE + chord_vec * xtol)[0]:
                delta = coords[ii + 1] - coords[ii]
                unit_delta = delta / np.linalg.norm(delta)

                if np.abs(np.dot(unit_chord_vec, unit_delta)) < tol:
                    TE_mask += [ii, ii + 1]
                else:
                    airfoil_mask += [ii, ii + 1]
            else:
                airfoil_mask += [ii, ii + 1]

        # list(set()) removes the duplicate pts
        self.recompute(coords[list(set(airfoil_mask))])

        return coords[list(set(TE_mask))]

    ## Sampling
    def getSampledPts(self, nPts, spacingFunc=sampling.polynomial, func_args=None, nTEPts=0, TE_knot=False):
        """
        This function defines the point sampling along the airfoil surface.

        Parameters
        ----------
        nPts: int
            Number of points to be sampled
        spacingFunc: function
            sampling function object. The methods available in :class:`prefoil.sampling` are a good default example.
        func_args: dictionary
            Dictionary of input arguments for the sampling function.
        nTEPts: float
            Number of points along the **blunt** trailing edge
        TE_knot: bool
            If True, add a duplicate point between the lower airfoil surface and the TE to indicate that a knot is present. If there is a sharp or round trailing edge then this does nothing.

        Returns
        -------
        coords : Ndarray [N, 2]
            Coordinates array, anticlockwise, from trailing edge
        """
        s = sampling.joinedSpacing(nPts, spacingFunc=spacingFunc, func_args=func_args, s_LE=self.s_LE)
        sampled_coords = self.spline.getValue(s)
        if not self.closedCurve and TE_knot:
            sampled_coords = np.vstack((sampled_coords, sampled_coords[-1]))

        if nTEPts and not self.closedCurve:
            coords_TE = np.zeros((nTEPts + 2, sampled_coords.shape[1]))
            for idim in range(sampled_coords.shape[1]):
                val1 = self.spline.getValue(1)[idim]
                val2 = self.spline.getValue(0)[idim]
                coords_TE[:, idim] = np.linspace(val1, val2, nTEPts + 2)
            sampled_coords = np.vstack((sampled_coords, coords_TE[1:-1]))

        if not self.closedCurve:
            sampled_coords = np.vstack((sampled_coords, sampled_coords[0]))

        self.sampled_pts = sampled_coords

        return sampled_coords

    def _buildFFD(self, nffd, fitted, xmargin, ymarginu, ymarginl, xslice, coords):
        """
        The function that actually builds the FFD Box from all of the given parameters

        Parameters
        ----------
        nffd : int
            number of FFD points along the chord

        fitted : bool
            flag to pick between a fitted FFD (True) and box FFD (False)

        xmargin : float
            The closest distance of the FFD box to the tip and aft of the airfoil

        ymarginu : float
            When a box ffd is generated this specifies the top of the box's y values as
            the maximum y value in the airfoil coordinates plus this margin.
            When a fitted ffd is generated this is the margin between the FFD point at
            an xslice location and the upper surface of the airfoil at this location

        ymarginl : float
            When a box ffd is generated this specifies the bottom of the box's y values as
            the minimum y value in the airfoil coordinates minus this margin.
            When a fitted ffd is generated this is the margin between the FFD point at
            an xslice location and the lower surface of the airfoil at this location

        xslice : Ndarray [N,2]
            User specified xslice locations. If this is chosen nffd is ignored

        coords : Ndarray [N,2]
            the coordinates to use for defining the airfoil, if the user does not
            want the original coordinates for the airfoil used. This shouldn't be
            used unless the user wants fine tuned control over the FFD creation,
            It should be sufficient to ignore.

        """

        if coords is None:
            coords = self.getSplinePts()

        if xslice is None:
            xslice = np.zeros(nffd)
            for i in range(nffd):
                xtemp = i * 1.0 / (nffd - 1.0)
                xslice[i] = min(coords[:, 0]) - 1.0 * xmargin + (max(coords[:, 0]) + 2.0 * xmargin) * xtemp
        else:
            nffd = len(xslice)

        FFDbox = np.zeros((nffd, 2, 2, 3))

        if fitted:
            ylower = np.zeros(nffd)
            yupper = np.zeros(nffd)
            for i in range(nffd):
                ymargin = ymarginu + (ymarginl - ymarginu) * xslice[i]
                yu, yl = _getClosestY(coords, xslice[i])
                yupper[i] = yu + ymargin
                ylower[i] = yl - ymargin
        else:
            yupper = np.ones(nffd) * (max(coords[:, 1]) + ymarginu)
            ylower = np.ones(nffd) * (min(coords[:, 1]) - ymarginl)

        # X
        FFDbox[:, 0, 0, 0] = xslice[:].copy()
        FFDbox[:, 1, 0, 0] = xslice[:].copy()
        # Y
        # lower
        FFDbox[:, 0, 0, 1] = ylower[:].copy()
        # upper
        FFDbox[:, 1, 0, 1] = yupper[:].copy()
        # copy
        FFDbox[:, :, 1, :] = FFDbox[:, :, 0, :].copy()
        # Z
        FFDbox[:, :, 0, 2] = 0.0
        # Z
        FFDbox[:, :, 1, 2] = 1.0

        return FFDbox

    ## Output
    def writeCoords(self, filename, coords=None, spline_coords=False, file_format="plot3d"):
        """
        Writes out a set of airfoil coordinates. By default, the most recently sampled coordinates are written out.
        If there are no recently sampled coordinates and none are passed in this will fail.

        Parameters
        ----------
        filename : str
            the filename without extension to write to

        coords : Ndarray [N,2]
            the coordinates to write out to a file. If None then the most recent sampled set of points is used.

        spline_coords : bool
            If true it will write out the underlying spline coordinates and the value of `coords` will be ignored. Useful if only geometric modifications to coordinates are being preformed.

        file_format : str
            the file format to write, can be `plot3d` or `dat`
        """

        if spline_coords is True:
            coords = self.getSplinePts()

        if coords is None:
            if self.sampled_pts is not None:
                coords = self.sampled_pts
            else:
                raise Error("No coordinates to write!")

        if file_format == "plot3d":
            _writePlot3D(filename, coords[:, 0], coords[:, 1])
        elif file_format == "dat":
            _writeDat(filename, coords[:, 0], coords[:, 1])
        else:
            raise Error(file_format + " is not a supported output format!")

    def generateFFD(
        self, nffd, filename, fitted=True, xmargin=0.001, ymarginu=0.02, ymarginl=0.02, xslice=None, coords=None
    ):
        """
        Generates an FFD from the airfoil and writes it out to file

        Parameters
        ----------
        nffd : int
            the number of chordwise points in the FFD

        filename : str
            filename to write out, not including the '.xyz' ending

        fitted : bool
            flag to pick between a fitted FFD (True) and box FFD (False)

        xmargin : float
            The closest distance of the FFD box to the tip and aft of the airfoil

        ymarginu : float
            When a box ffd is generated this specifies the top of the box's y values as
            the maximum y value in the airfoil coordinates plus this margin.
            When a fitted ffd is generated this is the margin between the FFD point at
            an xslice location and the upper surface of the airfoil at this location

        ymarginl : float
            When a box ffd is generated this specifies the bottom of the box's y values as
            the minimum y value in the airfoil coordinates minus this margin.
            When a fitted ffd is generated this is the margin between the FFD point at
            an xslice location and the lower surface of the airfoil at this location

        xslice : Ndarray [N,2]
            User specified xslice locations. If this is chosen nffd is ignored

        coords : Ndarray [N,2]
            the coordinates to use for defining the airfoil, if the user does not
            want the original coordinates for the airfoil used. This shouldn't be
            used unless the user wants fine tuned control over the FFD creation,
            It should be sufficient to ignore.
        """

        FFDbox = self._buildFFD(nffd, fitted, xmargin, ymarginu, ymarginl, xslice, coords)

        _writeFFD(FFDbox, filename)

    ## Utils
    # maybe remove and put into a separate location?
    def plot(self, camber=False):
        """
        Plots the airfoil.
        It tries to plot the most recently sampled set of points, but if none exists, it will plot the original set of coordinates.

        Parameters
        ----------
        camber : bool
            True to plot the camber line

        Returns
        -------
        fig : matplotlib.pyplot.Figure
            The figure with the plotted airfoil
        """
        try:
            import matplotlib.pyplot as plt
        except ImportError as e:
            raise ImportError("matplotlib is needed for the plotting functionality") from e

        if self.sampled_pts is None:
            coords = self.getSplinePts()
        else:
            coords = self.sampled_pts

        fig = plt.figure()
        # pts = self._getDefaultSampling(npts=1000)
        plt.plot(coords[:, 0], coords[:, 1], "-r")
        plt.axis("equal")
        # if self.sampled_X is not None:
        plt.plot(coords[:, 0], coords[:, 1], "o")

        if camber:
            camber_pts = self.camber.getValue(np.linspace(0, 1, 200))
            plt.plot(camber_pts[:, 0], camber_pts[:, 1], "--g", label="camber")

        return fig
